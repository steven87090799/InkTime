from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
from typing import Any, Callable

from inktime.app.core.paths import safe_join
from inktime.app.core.ai_trace import sanitize_ai_payload
from inktime.app.domain.analysis import (
    AnalysisValidationError,
    REPAIR_TOKEN_CAP,
    SCHEMA_VERSION,
    build_analysis_plan,
    canonical_json,
    fingerprint,
    normalize_analysis_plan,
    normalize_reasoning_effort,
    validate_analysis_result,
)
from inktime.app.domain.analysis.json_repair import extract_json_value
from inktime.app.domain.analysis.execution_mode import (
    execution_mode,
    permits_automatic_ai,
    permits_manual_ai,
)
from inktime.app.domain.analysis.scoring import (
    DEFAULT_FAVORITE_BONUS,
    DEFAULT_RANKING_WEIGHTS,
    calculate_ranking_score,
    calculate_travel_bonus,
    grade_to_score,
)
from inktime.app.domain.photos import ThumbnailCache
from inktime.app.domain.photos.quality_policy import FEATURE_VERSION, evaluate_local_quality
from inktime.app.providers.base import ProviderResponse, Usage, VisionAttemptState, VisionProvider
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.ai_traces import AITraceRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.budgets import BudgetService


class AnalysisDisabledError(RuntimeError):
    """Frozen disabled jobs must never create a new analysis record."""

    code = "ANALYSIS-DISABLED"


PROMPT_VERSION = "photo-quality-v5-grade-anchors"
FULL_ANALYSIS_TOKEN_CAP = 2048
CAPTION_VARIANTS_TOKEN_CAP = 3072


class ProviderUnavailableError(ValueError):
    code = "VLM-008"


def _unknown_visual_orientation() -> dict:
    """Local-only results deliberately have no authoritative orientation advice."""
    return {
        "rotation_cw": None,
        "confidence": 0.0,
        "ambiguous": True,
        "evidence": ["insufficient_visual_cues"],
    }


class PhotoAnalysisService:
    def __init__(
        self,
        photos: PhotoRepository,
        usage: UsageRepository,
        thumbnails: ThumbnailCache,
        budgets: BudgetService | None = None,
        settings: SettingsRepository | None = None,
        observability=None,
        process_boundary=None,
        traces: AITraceRepository | None = None,
    ) -> None:
        self.photos = photos
        self.usage = usage
        self.thumbnails = thumbnails
        self.budgets = budgets
        self.settings = settings or (budgets.settings if budgets else None)
        self.observability = observability
        self.process_boundary = process_boundary
        self.traces = traces

    def _activity(self, severity: str, event: str, message: str, **fields) -> None:
        if self.observability is not None:
            self.observability.record(severity, "analysis", event, message, **fields)

    def _trace_write(self, method: str, *args, **kwargs):
        """Trace persistence is fail-open and never owns provider retry behavior."""

        if self.traces is None:
            return None
        try:
            return getattr(self.traces, method)(*args, **kwargs)
        except Exception as exc:
            try:
                self._activity(
                    "WARNING",
                    "ai_trace_persist_failed",
                    "AI Trace 寫入失敗；分析流程不受影響",
                    error_code="AI-TRACE-PERSIST",
                    operation=method,
                    error=str(exc)[:500],
                )
            except Exception:  # noqa: S110 -- diagnostics must not affect analysis ownership
                pass
            return None

    @staticmethod
    def _trace_retry_delay_ms(exc: Exception) -> int | None:
        try:
            value = getattr(exc, "retry_after", None)
            return max(0, int(float(value) * 1000)) if value is not None else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _caption_controls(self) -> dict | None:
        if self.settings is None or not bool(self.settings.get("analysis.advanced_caption_enabled", False)):
            return None
        settings = self.settings

        def lines(key: str) -> list[str]:
            return [line.strip() for line in str(settings.get(key, "")).splitlines() if line.strip()]

        return {
            "caption_min_chars": int(settings.get("analysis.caption_min_chars", 120)),
            "caption_target_chars": int(settings.get("analysis.caption_target_chars", 160)),
            "caption_max_chars": int(settings.get("analysis.caption_max_chars", 220)),
            "side_caption_min_chars": int(settings.get("analysis.side_caption_min_chars", 8)),
            "side_caption_target_chars": int(settings.get("analysis.side_caption_target_chars", 12)),
            "side_caption_max_chars": int(settings.get("analysis.side_caption_max_chars", 16)),
            "copy_default_style": str(settings.get("analysis.copy_default_style", "natural")),
            "copy_humor_level": int(settings.get("analysis.copy_humor_level", 1)),
            "copy_poetic_level": int(settings.get("analysis.copy_poetic_level", 1)),
            "copy_avoid_cliche": bool(settings.get("analysis.copy_avoid_cliche", True)),
            "copy_avoid_direct_description": bool(
                settings.get("analysis.copy_avoid_direct_description", True)
            ),
            "copy_forbid_exclamation": bool(settings.get("analysis.copy_forbid_exclamation", True)),
            "copy_forbid_like_phrase": bool(settings.get("analysis.copy_forbid_like_phrase", True)),
            "copy_max_commas": int(settings.get("analysis.copy_max_commas", 2)),
            "copy_avoid_abstract_ending": bool(settings.get("analysis.copy_avoid_abstract_ending", True)),
            "copy_banned_words": lines("analysis.copy_banned_words"),
            "copy_banned_patterns": lines("analysis.copy_banned_patterns"),
            "copy_custom_rules": str(self.settings.get("analysis.copy_custom_rules", "")),
            "caption_variants_enabled": bool(self.settings.get("analysis.caption_variants_enabled", False)),
        }

    @staticmethod
    def _caption_generation_controls(controls: dict | None) -> dict | None:
        if not controls:
            return None
        return {key: value for key, value in controls.items() if key != "copy_default_style"}

    @staticmethod
    def _caption_display_controls(controls: dict | None) -> dict | None:
        if not controls:
            return None
        return {"copy_default_style": str(controls.get("copy_default_style", "natural"))}

    def build_plan(self, *, strategy: str, provider_route: list[dict], scoring_profile: dict) -> dict:
        """Build the sole server-authoritative non-secret Analysis Plan."""
        if self.settings is None:
            raise RuntimeError("分析設定尚未初始化")
        settings = self.settings
        controls = self._caption_controls()
        generation_controls = self._caption_generation_controls(controls)
        display_controls = self._caption_display_controls(controls)
        prompt_version = self._prompt_version(generation_controls)
        prefilter = {
            "enabled": bool(settings.get("analysis.prefilter_enabled", True)),
            "screenshots_enabled": bool(settings.get("analysis.prefilter_screenshots", True)),
            "low_quality_enabled": bool(settings.get("analysis.prefilter_low_quality", True)),
            "sensitivity": str(settings.get("analysis.prefilter_sensitivity", "conservative")),
            "e6_enabled": bool(settings.get("analysis.e6_prefilter_enabled", True)),
            "e6_min_score": float(settings.get("analysis.e6_min_score", 25)),
        }
        execution_policy = {
            "execution_mode": execution_mode(settings),
            "ai_mode": str(settings.get("analysis.ai_mode", "top_candidates")),
            "top_n": int(settings.get("analysis.ai_top_n", 50)),
            "daily_photo_limit": int(settings.get("analysis.ai_daily_photo_limit", 50)),
            "monthly_photo_limit": int(settings.get("analysis.ai_monthly_photo_limit", 500)),
        }
        travel_policy = {
            "enabled": bool(settings.get("travel_bonus_enabled", True)),
            "home_latitude": settings.get("home_latitude"),
            "home_longitude": settings.get("home_longitude"),
            "home_radius_km": float(settings.get("home_radius_km", 60)),
            "near_bonus": float(settings.get("travel_bonus_near", 2)),
            "far_bonus": float(settings.get("travel_bonus_far", 4)),
            "foreign_bonus": float(settings.get("foreign_country_bonus", 6)),
            "rare_bonus": float(settings.get("rare_location_bonus", 2)),
            "maximum_bonus": float(settings.get("max_total_bonus", 8)),
            "location_rule_version": str(settings.get("location_rule_version", "travel-v1")),
        }
        analysis_model_value = settings.get("model.analysis_model", None)
        legacy_model_value = settings.get("model.high_model", None)
        # Existing installations may have a customized legacy high model.  A
        # newly inserted canonical default must not silently discard it; once
        # the new key is explicitly customized, it becomes authoritative.
        legacy_is_authoritative = (
            callable(getattr(settings, "is_explicit", None))
            and not settings.is_explicit("model.analysis_model")
            and settings.is_explicit("model.high_model")
        )
        analysis_model = str(
            (legacy_model_value if legacy_is_authoritative else analysis_model_value)
            or legacy_model_value
            or settings.get("model.low_model", "high-quality-vision")
        )
        repair_policy = {
            "enabled": True,
            "model": str(settings.get("model.repair_model", analysis_model) or analysis_model),
            "max_tokens": max(
                256,
                min(REPAIR_TOKEN_CAP, int(settings.get("budget.repair_max_tokens", REPAIR_TOKEN_CAP))),
            ),
            "max_attempts": 1,
            "text_only": True,
        }
        return build_analysis_plan(
            strategy=strategy,
            provider_route=provider_route,
            low_model=str(settings.get("model.low_model", "low-cost-vision")),
            high_model=analysis_model,
            stage_two_threshold=float(settings.get("analysis.stage_two_threshold", 65)),
            favorite_override=bool(settings.get("analysis.favorite_override", True)),
            scoring_profile=scoring_profile,
            caption_controls=generation_controls,
            prompt_version=prompt_version,
            high_image_max_side=int(
                settings.get("analysis.image_max_side", settings.get("analysis.high_image_max_side", 1024))
            ),
            caption_display_controls=display_controls,
            prefilter=prefilter,
            execution_policy=execution_policy,
            travel_policy=travel_policy,
            scoring_rules=str(settings.get("analysis.scoring_rules", "")),
            reasoning_effort=normalize_reasoning_effort(settings.get("batch.reasoning_effort", "none")),
            repair_policy=repair_policy,
        )

    @staticmethod
    def _prompt_version(caption_controls: dict | None) -> str:
        if not caption_controls:
            return PROMPT_VERSION
        generation_controls = {
            key: value for key, value in caption_controls.items() if key != "copy_default_style"
        }
        fingerprint = hashlib.sha256(
            json.dumps(generation_controls, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        return f"{PROMPT_VERSION}-caption-{fingerprint}"

    @staticmethod
    def _apply_caption_variant(result: dict, caption_controls: dict | None) -> dict:
        if not caption_controls or not caption_controls["caption_variants_enabled"]:
            return result
        variants = (result.get("details") or {}).get("caption_variants") or {}
        style = str(caption_controls["copy_default_style"])
        selected = (
            variants.get(style)
            or variants.get("natural")
            or result.get("side_caption")
            or "畫面把此刻收好了。"
        )
        result["side_caption"] = str(selected).strip()
        return result

    @staticmethod
    def _local_result(photo) -> dict:
        quality = max(0.0, min(100.0, float(photo["blur_score"] or 0) ** 0.5 * 4))
        screenshot = float(photo["screenshot_likelihood"] or 0) >= 0.65
        return {
            "schema_version": 2,
            "caption": "已完成本地影像特徵分析，未將照片傳送至模型。",
            "types": ["截圖" if screenshot else "其他"],
            "memory_score": 10.0 if screenshot else 50.0,
            "beauty_score": quality,
            "technical_quality_score": quality,
            "emotion_score": 0.0,
            "side_caption": "",
            "should_keep": not screenshot,
            "sensitive": False,
            "reason": "依本地清晰度、曝光與截圖特徵判定",
            "visual_orientation": _unknown_visual_orientation(),
        }

    @staticmethod
    def _local_quality(photo) -> float:
        blur = max(0.0, float(photo["blur_score"] or 0))
        contrast = max(0.0, min(100.0, float(photo["contrast"] or 0)))
        exposure_penalty = max(
            float(photo["overexposed_ratio"] or 0),
            float(photo["underexposed_ratio"] or 0),
        )
        return round(
            max(0.0, min(100.0, blur**0.5 * 3.2 + contrast * 0.8 - exposure_penalty * 45)),
            2,
        )

    def prefilter_snapshot(self, photo, *, policy_settings: dict | None = None) -> dict:
        policy_settings = policy_settings or {
            key: self.settings.get(key, default) if self.settings is not None else default
            for key, default in (
                ("analysis.prefilter_enabled", True),
                ("analysis.prefilter_screenshots", True),
                ("analysis.prefilter_low_quality", True),
                ("analysis.prefilter_sensitivity", "conservative"),
                ("analysis.e6_prefilter_enabled", True),
                ("analysis.e6_min_score", 25),
            )
        }
        policy = evaluate_local_quality(dict(photo), settings=policy_settings)
        labels = {
            "screenshot_strong": "明確截圖證據",
            "screenshot_score": "截圖信號分數",
            "screenshot_independent_signals": "截圖獨立信號",
            "document_token_with_evidence": "文件或掃描證據",
            "severe_blur": "嚴重模糊或失焦",
            "suspected_blur": "疑似模糊",
            "short_edge_under_240": "解析度過低（短邊）",
            "tiny_empty": "極小且近乎空白",
            "small_compressed": "小型壓縮檔",
            "extreme_exposure_low_contrast": "極端曝光且低對比",
            "exposure_low_priority": "曝光比例偏高",
            "social_export": "社群平台轉存",
        }
        checks = [
            {"key": key, "label": labels.get(key, key), "hit": bool(hit)}
            for key, hit in policy["evidence"]["checks"].items()
        ]
        return {
            "enabled": bool(policy_settings["analysis.prefilter_enabled"]),
            "sensitivity": policy["sensitivity"],
            "feature_version": policy["feature_version"],
            "decision": policy["decision"],
            "excluded": policy["decision"] == "auto_excluded",
            "primary_reason": policy["primary_reason"],
            "matched_checks": policy["matched_checks"],
            "thresholds": policy["thresholds"],
            "e6_threshold": policy["e6_threshold"],
            "e6_feature_version": policy["e6_feature_version"],
            "evidence": policy["evidence"],
            "checks": checks,
            "summary": f"本機品質規則：{policy['decision']}（{policy['primary_reason']}）",
        }

    def _prefilter_result(self, photo, *, policy_settings: dict | None = None) -> dict | None:
        evaluation = self.prefilter_snapshot(photo, policy_settings=policy_settings)
        if not evaluation["excluded"]:
            return None

        quality = self._local_quality(photo)
        if evaluation["primary_reason"] == "screenshot":
            label = "截圖"
            reasons = ["本機截圖特徵達排除門檻"]
            memory_score = 5.0
            types = ["截圖"]
        elif evaluation["primary_reason"] == "e6_below_threshold":
            label = "不適合 E6 六色顯示的照片"
            reasons = ["六色量化後對比、主體、膚色或細節保留不足"]
            memory_score = 20.0
            types = ["其他"]
        else:
            label = "明顯低品質照片"
            reasons = evaluation["matched_checks"]
            memory_score = 15.0
            types = ["其他"]
        return {
            "schema_version": 2,
            "caption": f"本機預篩選已排除{label}，未將圖片傳送至模型。",
            "types": types,
            "memory_score": memory_score,
            "beauty_score": quality,
            "technical_quality_score": quality,
            "emotion_score": 0.0,
            "side_caption": "",
            "should_keep": False,
            "sensitive": False,
            "reason": "、".join(reasons),
            "visual_orientation": _unknown_visual_orientation(),
        }

    def _ensure_e6_suitability(self, photo_id: str, photo, source: Path):
        # E6 is a bounded Scanner feature.  Analysis must not reopen the
        # original image merely to backfill an old row.
        return photo

    def _ai_mode(self, execution_policy: dict | None = None) -> str:
        if execution_policy:
            return str(execution_policy.get("ai_mode", "top_candidates"))
        return str(self.settings.get("analysis.ai_mode", "top_candidates")) if self.settings else "legacy"

    def _allow_ai_for_photo(
        self, photo_id: str, *, force_ai: bool, execution_policy: dict | None = None
    ) -> bool:
        execution = str(
            (execution_policy or {}).get("execution_mode")
            or (execution_mode(self.settings) if self.settings is not None else "automatic_ai")
        )
        if force_ai:
            return permits_manual_ai(execution)
        if not permits_automatic_ai(execution):
            return False
        mode = self._ai_mode(execution_policy)
        if mode == "off":
            return False
        if mode == "on_demand":
            return False
        if mode == "top_candidates":
            limit = int((execution_policy or {}).get("top_n", 50))
            return self.photos.is_top_candidate(photo_id, limit)
        return mode in {"eligible", "full_library", "legacy"}

    def _photo_limits_reached(self, execution_policy: dict | None = None) -> bool:
        if self.settings is None:
            return False
        policy = execution_policy or {}
        return self.photos.ai_limit_reached(
            daily_limit=int(
                policy.get("daily_photo_limit", self.settings.get("analysis.ai_daily_photo_limit", 50))
            ),
            monthly_limit=int(
                policy.get("monthly_photo_limit", self.settings.get("analysis.ai_monthly_photo_limit", 500))
            ),
        )

    def _score_result(
        self,
        result: dict,
        photo,
        *,
        ranking_weights: dict[str, float],
        favorite_bonus: float,
        travel_policy: dict | None = None,
    ) -> dict:
        details = result.get("details") or {}
        for target, grade_key in (
            ("memory_score", "memory_grade"),
            ("beauty_score", "beauty_grade"),
            ("technical_quality_score", "technical_grade"),
            ("emotion_score", "emotion_grade"),
        ):
            fallback_grade = "aesthetic_grade" if grade_key == "beauty_grade" else grade_key
            result[target] = grade_to_score(
                details.get(grade_key, details.get(fallback_grade)), float(result[target])
            )
        base = calculate_ranking_score(
            result,
            ranking_weights,
            favorite=bool(photo["favorite"]),
            favorite_bonus=favorite_bonus,
        )
        travel_bonus = 0.0
        location_rule_version = None
        policy = travel_policy or {}
        travel_enabled = (
            bool(policy.get("enabled"))
            if policy
            else (self.settings is not None and bool(self.settings.get("travel_bonus_enabled", True)))
        )
        if travel_enabled:
            country = str(details.get("country_candidate") or "").strip().casefold()
            foreign = bool(country) and country not in {"tw", "taiwan", "台灣", "臺灣", "中華民國"}
            visits = self.photos.location_visit_count(photo["gps_lat"], photo["gps_lon"])
            if policy:
                home_latitude = policy.get("home_latitude")
                home_longitude = policy.get("home_longitude")
                home_radius_km = float(policy.get("home_radius_km", 60))
                near_bonus = float(policy.get("near_bonus", 2))
                far_bonus = float(policy.get("far_bonus", 4))
                foreign_bonus = float(policy.get("foreign_bonus", 6))
                rare_bonus = float(policy.get("rare_bonus", 2))
                maximum_bonus = float(policy.get("maximum_bonus", 8))
                location_rule_version = str(policy.get("location_rule_version", "travel-v1"))
            else:
                assert self.settings is not None
                home_latitude = self.settings.get("home_latitude")
                home_longitude = self.settings.get("home_longitude")
                home_radius_km = float(self.settings.get("home_radius_km", 60))
                near_bonus = float(self.settings.get("travel_bonus_near", 2))
                far_bonus = float(self.settings.get("travel_bonus_far", 4))
                foreign_bonus = float(self.settings.get("foreign_country_bonus", 6))
                rare_bonus = float(self.settings.get("rare_location_bonus", 2))
                maximum_bonus = float(self.settings.get("max_total_bonus", 8))
                location_rule_version = str(self.settings.get("location_rule_version", "travel-v1"))
            travel_bonus, _distance = calculate_travel_bonus(
                latitude=photo["gps_lat"],
                longitude=photo["gps_lon"],
                home_latitude=home_latitude,
                home_longitude=home_longitude,
                home_radius_km=home_radius_km,
                near_bonus=near_bonus,
                far_bonus=far_bonus,
                foreign_bonus=foreign_bonus,
                rare_bonus=rare_bonus,
                foreign_country=foreign,
                rare_location=0 < visits <= 3,
                maximum=maximum_bonus,
            )
        result["local_score"] = float(photo["local_candidate_score"] or 0.0)
        result["semantic_score"] = base
        result["base_ranking_score"] = base
        result["travel_bonus"] = travel_bonus
        result["final_ranking_score"] = round(min(100.0, base + travel_bonus), 2)
        result["ranking_score"] = result["final_ranking_score"]
        result["location_rule_version"] = location_rule_version
        return result

    def _save_result(
        self,
        *,
        photo_id: str,
        job_id: str | None,
        stage: str,
        provider: str,
        model: str,
        result: dict,
        raw: str,
        photo,
        ranking_weights: dict[str, float],
        favorite_bonus: float,
        scoring_version_id: str | None,
        schema_kind: str,
        prompt_version: str = PROMPT_VERSION,
        analysis_fingerprint: str | None = None,
        analysis_spec_json: str | None = None,
        vision_request_fingerprint: str | None = None,
        vision_input_spec_json: str | None = None,
        prefilter_evaluation: dict | None = None,
        travel_policy: dict | None = None,
        analysis_source: str = "direct",
        connection=None,
        trace_id: str | None = None,
    ) -> dict:
        ranked = self._score_result(
            result,
            photo,
            ranking_weights=ranking_weights,
            favorite_bonus=favorite_bonus,
            travel_policy=travel_policy,
        )
        if trace_id:
            self._trace_write("add_event", trace_id, "SCORE_CALIBRATED")
        self.photos.save_analysis(
            photo_id,
            job_id,
            stage,
            provider,
            model,
            ranked,
            raw,
            analysis_source,
            ranking_score=ranked["ranking_score"],
            scoring_version_id=scoring_version_id,
            schema_kind=schema_kind,
            local_score=ranked["local_score"],
            semantic_score=ranked["semantic_score"],
            base_ranking_score=ranked["base_ranking_score"],
            final_ranking_score=ranked["final_ranking_score"],
            travel_bonus=ranked["travel_bonus"],
            location_rule_version=ranked["location_rule_version"],
            prompt_version=prompt_version,
            analysis_fingerprint=analysis_fingerprint,
            analysis_spec_json=analysis_spec_json,
            vision_request_fingerprint=vision_request_fingerprint,
            vision_input_spec_json=vision_input_spec_json,
            prefilter_evaluation=prefilter_evaluation,
            connection=connection,
        )
        self._activity(
            "DEBUG",
            "caption_analysis_completed",
            "Caption 分析完成",
            job_id=job_id,
            photo_id=photo_id,
            stage=stage,
            trace_id=prompt_version,
        )
        return ranked

    def _record(
        self,
        provider: VisionProvider,
        model: str,
        job_id: str | None,
        photo_id: str,
        request_type: str,
        response: ProviderResponse,
        started_at: str,
        started_perf: float,
        retry_count: int = 0,
    ) -> tuple[float, int]:
        estimated_cost = provider.estimate_cost(model, response.usage)
        provider_cost = response.usage.provider_reported_cost
        if provider_cost is not None:
            actual_cost = max(0.0, float(provider_cost))
            cost_source = "provider_reported"
        elif estimated_cost is not None:
            actual_cost = None
            cost_source = "estimated"
        else:
            actual_cost = None
            cost_source = "unknown"
        effective_cost = (
            max(0.0, float(provider_cost))
            if provider_cost is not None
            else max(0.0, float(estimated_cost))
            if estimated_cost is not None
            else 0.0
        )
        metrics = dict(response.request_metrics or getattr(provider, "last_request_metrics", {}) or {})
        usage_id = self.usage.record(
            provider=provider.name,
            provider_id=str(getattr(provider, "provider_id", provider.name)),
            model=model,
            job_id=job_id,
            photo_id=photo_id,
            request_type=request_type,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_tokens=response.usage.cached_tokens,
            estimated_cost=estimated_cost,
            actual_cost=actual_cost,
            started_at=started_at,
            latency_ms=int((time.perf_counter() - started_perf) * 1000),
            status="completed",
            retry_count=retry_count,
            request_id=response.request_id,
            reasoning_tokens=response.usage.reasoning_tokens,
            cache_write_tokens=response.usage.cache_write_tokens,
            cost_source=cost_source,
            prompt_chars=metrics.get("prompt_chars", 0),
            schema_chars=metrics.get("schema_chars", 0),
            request_body_bytes=metrics.get("request_body_bytes", 0),
            image_bytes=metrics.get("image_bytes", 0),
        )
        return effective_cost, usage_id

    def _model_call(
        self,
        *,
        provider: VisionProvider,
        image_factory: Callable[[], Any],
        model: str,
        detail: str,
        stage: str,
        job_id: str | None,
        photo_id: str,
        content_sha256: str,
        schema_kind: str,
        reasoning_effort: str = "none",
        caption_controls: dict | None,
        repair_policy: dict | None,
        prompt_version: str,
        vision_input: dict,
        analysis_fingerprint: str | None = None,
        photo_metadata: dict[str, Any] | None = None,
        provider_prompt_contract_sha256: str | None = None,
        force_recompute: bool = False,
        _excluded_providers: set[str] | None = None,
        _trace_id: str | None = None,
    ) -> tuple[dict, str, float, bool, str, str, str, str, Usage, int, str | None]:
        selected_channel = None
        selected_provider = provider
        # Enumerate Frozen Route identities before consulting network state.
        # A cache hit is valid even while its provider is circuit-open or rate
        # limited, so it must never be hidden by candidate_channels().
        selector = getattr(provider, "route_channels", None)
        if not callable(selector):
            selector = getattr(provider, "candidate_channels", None)
        if callable(selector):
            candidates = selector(excluded=_excluded_providers)
            if not candidates:
                raise ValueError("VLM-005 所有 Provider 暫時不可用或已達 Rate Limit")
            selected_channel = candidates[0]
            selected_provider = selected_channel.provider
        actual_provider = str(getattr(selected_provider, "provider_id", selected_provider.name))

        fingerprint_material = {
            "content_sha256": content_sha256,
            "actual_provider": actual_provider,
            "model": model,
            "prompt_version": prompt_version,
            "schema_kind": schema_kind,
            "reasoning_effort": reasoning_effort,
            **vision_input,
        }
        if provider_prompt_contract_sha256:
            fingerprint_material["provider_prompt_contract_sha256"] = str(provider_prompt_contract_sha256)
        request_fingerprint = fingerprint(
            {
                **fingerprint_material,
                "schema_version": SCHEMA_VERSION if schema_kind == "full" else 2,
            }
        )
        # Before the v3 contract, full analyses used schema_version=2 in the
        # request fingerprint.  Keep that exact identity for backward lookup;
        # new successful writes still use the canonical v3 fingerprint.
        legacy_v2_fingerprint = fingerprint({**fingerprint_material, "schema_version": 2})
        # Legacy schema constrains this column to basic/full; the v4 Vision
        # Request Fingerprint is the authoritative additional cache dimension.
        cache_schema_kind = schema_kind
        has_prompt_contract = bool(provider_prompt_contract_sha256)
        cache_schema_versions: tuple[int, ...]
        if has_prompt_contract:
            cache_schema_versions = (SCHEMA_VERSION,) if schema_kind == "full" else (2,)
        else:
            cache_schema_versions = (SCHEMA_VERSION, 2) if schema_kind == "full" else (2,)
        cache_schema_version = SCHEMA_VERSION if has_prompt_contract and schema_kind == "full" else None
        vision_json = canonical_json(vision_input)

        def get_cache() -> dict | None:
            for cache_schema_version in cache_schema_versions:
                fingerprints = (
                    (request_fingerprint,)
                    if has_prompt_contract or cache_schema_version != 2
                    else (request_fingerprint, legacy_v2_fingerprint)
                )
                for cache_fingerprint in fingerprints:
                    cached_row = self.photos.get_ai_cache(
                        content_sha256=content_sha256,
                        provider=actual_provider,
                        model_name=model,
                        prompt_version=prompt_version,
                        schema_version=cache_schema_version,
                        schema_kind=cache_schema_kind,
                        vision_request_fingerprint=cache_fingerprint,
                    )
                    if cached_row is not None:
                        return cached_row
            return None

        baseline_cache_created_at: str | None = None
        if force_recompute:
            baseline = get_cache()
            if baseline is not None:
                baseline_cache_created_at = str(baseline["created_at"])
        if not force_recompute:
            cached = get_cache()
            if cached is not None:
                try:
                    self._activity(
                        "DEBUG",
                        "caption_cache_hit",
                        "Caption AI Cache 命中",
                        job_id=job_id,
                        photo_id=photo_id,
                        stage=stage,
                        trace_id=request_fingerprint,
                    )
                    return (
                        validate_analysis_result(cached["result"]),
                        str(cached["raw_json"]),
                        0.0,
                        True,
                        actual_provider,
                        model,
                        request_fingerprint,
                        vision_json,
                        Usage(),
                        0,
                        None,
                    )
                except AnalysisValidationError:
                    pass

        def is_force_generation(cache_row: dict) -> bool:
            return (
                not force_recompute
                or baseline_cache_created_at is None
                or str(cache_row["created_at"]) != baseline_cache_created_at
            )

        cache_key = request_fingerprint
        self._activity(
            "DEBUG",
            "caption_cache_miss",
            "Caption AI Cache 未命中",
            job_id=job_id,
            photo_id=photo_id,
            stage=stage,
            trace_id=prompt_version,
        )
        owner_id = str(uuid4())
        provider_timeout = max(5, int(getattr(selected_provider, "timeout", 120)))
        # A legal owner may need one vision call and one JSON repair.  Wait no
        # less than that bounded lease, so a healthy owner is never displaced.
        reservation_lease_seconds = provider_timeout * 2 + max(10, provider_timeout // 10)
        deadline = time.monotonic() + reservation_lease_seconds
        waited_for_owner = False
        while not self.photos.acquire_ai_cache_reservation(
            cache_key, owner_id, lease_seconds=reservation_lease_seconds
        ):
            waited_for_owner = True
            if time.monotonic() >= deadline:
                raise TimeoutError("AI-CACHE-001 等待相同分析結果逾時")
            time.sleep(0.05)
            # A forced request ignores a pre-existing entry, but once another
            # forced owner has completed its fresh result, all waiters share it.
            cached = get_cache()
            if cached is not None and is_force_generation(cached):
                self._activity(
                    "DEBUG",
                    "caption_cache_hit",
                    "等待中的 Caption AI Cache 已完成",
                    job_id=job_id,
                    photo_id=photo_id,
                    stage=stage,
                    trace_id=prompt_version,
                )
                return (
                    validate_analysis_result(cached["result"]),
                    str(cached["raw_json"]),
                    0.0,
                    True,
                    actual_provider,
                    model,
                    request_fingerprint,
                    vision_json,
                    Usage(),
                    0,
                    None,
                )
        if not force_recompute or waited_for_owner:
            cached = get_cache()
            if cached is not None and is_force_generation(cached):
                self.photos.finish_ai_cache_reservation(cache_key, owner_id)
                return (
                    validate_analysis_result(cached["result"]),
                    str(cached["raw_json"]),
                    0.0,
                    True,
                    actual_provider,
                    model,
                    request_fingerprint,
                    vision_json,
                    Usage(),
                    0,
                    None,
                )
        try:
            vision_attempt = VisionAttemptState()
            trace_id = _trace_id or str(uuid4())
            # The only owner generates a JPEG, after both cache checks.
            with image_factory() as image:
                result, raw, cost, usage, latency = self._perform_uncached_model_call(
                    provider=provider,
                    selected_channel=selected_channel,
                    image=image,
                    model=model,
                    detail=detail,
                    stage=stage,
                    job_id=job_id,
                    photo_id=photo_id,
                    content_sha256=content_sha256,
                    schema_kind=schema_kind,
                    reasoning_effort=reasoning_effort,
                    caption_controls=caption_controls,
                    repair_policy=repair_policy,
                    prompt_version=prompt_version,
                    cache_schema_kind=cache_schema_kind,
                    cache_schema_version=cache_schema_version,
                    vision_request_fingerprint=request_fingerprint,
                    vision_input_spec_json=vision_json,
                    cache_provider_identity=actual_provider,
                    vision_attempt=vision_attempt,
                    provider_request_context_id=(
                        f"{stage}|{job_id or 'manual'}|{photo_id}|{request_fingerprint}|"
                        f"{hashlib.sha256(owner_id.encode('utf-8')).hexdigest()[:16]}"
                    ),
                    trace_id=trace_id,
                    analysis_fingerprint=analysis_fingerprint,
                    photo_metadata=photo_metadata,
                )
        except Exception as exc:
            self.photos.finish_ai_cache_reservation(cache_key, owner_id, error=str(exc))
            record_outcome = getattr(self.photos, "record_analysis_request_outcome", None)
            if callable(record_outcome):
                ambiguous = bool(getattr(exc, "ambiguous", False))
                try:
                    record_outcome(
                        photo_id=photo_id,
                        job_id=job_id,
                        provider=actual_provider,
                        model=model,
                        request_fingerprint=request_fingerprint,
                        outcome="ambiguous_failed" if ambiguous else "failed",
                        error_code=getattr(exc, "code", None),
                        error_message=str(exc),
                        requires_manual_confirmation=ambiguous,
                    )
                except Exception as persist_error:
                    # The original provider failure remains authoritative; a
                    # telemetry write must never cause a second image call.
                    self._activity(
                        "ERROR",
                        "analysis_outcome_persist_failed",
                        "AI 請求結果無法持久化",
                        job_id=job_id,
                        photo_id=photo_id,
                        error=str(persist_error)[:500],
                    )
            self._trace_write(
                "mark_trace",
                trace_id,
                status="TIMEOUT" if isinstance(exc, TimeoutError) else "FAILED",
                error_code=str(getattr(exc, "code", "AI-PROVIDER-UNAVAILABLE")),
                error_message=str(exc),
            )
            # A timeout/connection failure after a vision POST may have
            # completed remotely.  Keep the reservation error visible and
            # never send the same image to another Provider automatically.
            if vision_attempt.vision_started or bool(getattr(exc, "vision_started", False)) or bool(
                getattr(exc, "ambiguous", False)
            ):
                raise
            channels = getattr(provider, "channels", ())
            excluded = set(_excluded_providers or ()) | {actual_provider}
            if selected_channel is not None and len(excluded) < len(channels):
                return self._model_call(
                    provider=provider,
                    image_factory=image_factory,
                    model=model,
                    detail=detail,
                    stage=stage,
                    job_id=job_id,
                    photo_id=photo_id,
                    content_sha256=content_sha256,
                    schema_kind=schema_kind,
                    reasoning_effort=reasoning_effort,
                    caption_controls=caption_controls,
                    repair_policy=repair_policy,
                    prompt_version=prompt_version,
                    vision_input=vision_input,
                    provider_prompt_contract_sha256=provider_prompt_contract_sha256,
                    force_recompute=force_recompute,
                    analysis_fingerprint=analysis_fingerprint,
                    photo_metadata=photo_metadata,
                    _excluded_providers=excluded,
                    _trace_id=trace_id,
                )
            raise
        self.photos.finish_ai_cache_reservation(cache_key, owner_id)
        return (
            result,
            raw,
            cost,
            False,
            actual_provider,
            model,
            request_fingerprint,
            vision_json,
            usage,
            latency,
            trace_id,
        )

    def _perform_uncached_model_call(
        self,
        *,
        provider: VisionProvider,
        selected_channel=None,
        image: Path,
        model: str,
        detail: str,
        stage: str,
        job_id: str | None,
        photo_id: str,
        content_sha256: str,
        schema_kind: str,
        reasoning_effort: str,
        caption_controls: dict | None,
        repair_policy: dict | None,
        prompt_version: str,
        cache_schema_kind: str,
        cache_schema_version: int | None,
        vision_request_fingerprint: str,
        vision_input_spec_json: str,
        cache_provider_identity: str,
        vision_attempt: VisionAttemptState,
        provider_request_context_id: str,
        trace_id: str,
        analysis_fingerprint: str | None,
        photo_metadata: dict[str, Any] | None,
    ) -> tuple[dict, str, float, Usage, int]:
        if self.budgets:
            self.budgets.assert_request_allowed(job_id, photo_id)
        started_at = datetime.now(timezone.utc).isoformat()
        started_perf = time.perf_counter()
        settings = self.budgets.settings if self.budgets else self.settings
        global_token_cap = int(settings.get("budget.max_tokens", 8000)) if settings else 8000
        requested_token_cap = int(
            settings.get(
                "budget.caption_variants_max_tokens"
                if caption_controls and caption_controls.get("caption_variants_enabled")
                else "budget.full_analysis_max_tokens",
                3072 if caption_controls and caption_controls.get("caption_variants_enabled") else 2048,
            )
        ) if settings else 2048
        hard_cap = (
            CAPTION_VARIANTS_TOKEN_CAP
            if caption_controls and caption_controls.get("caption_variants_enabled")
            else FULL_ANALYSIS_TOKEN_CAP
        )
        max_tokens = max(256, min(global_token_cap, requested_token_cap, hard_cap))
        concrete_provider = selected_channel.provider if selected_channel is not None else provider
        provider_identity = str(getattr(concrete_provider, "provider_id", concrete_provider.name))
        self._trace_write(
            "start_trace",
            trace_id=trace_id,
            job_id=job_id,
            photo_id=photo_id,
            provider=provider_identity,
            model=model,
            stage=stage,
            prompt_version=prompt_version,
            analysis_fingerprint=analysis_fingerprint,
            started_at=started_at,
        )
        attempt_id = self._trace_write(
            "start_attempt",
            trace_id=trace_id,
            provider=provider_identity,
            model=model,
            started_at=started_at,
        )
        self._activity(
            "DEBUG",
            "provider_request_started",
            "Caption Provider 請求開始",
            job_id=job_id,
            photo_id=photo_id,
            stage=stage,
            trace_id=trace_id,
            provider=provider_identity,
            model=model,
        )
        try:
            call = {
                "image_path": image,
                "model": model,
                "detail": detail,
                "stage": stage,
                "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort,
                "caption_controls": caption_controls,
                "provider_request_context_id": provider_request_context_id,
            }
            if self.process_boundary is None:
                # Cooperative providers may use this mutable marker to tell
                # the caller exactly when their transport has been handed the
                # image.  It is deliberately not sent into a child process.
                call["vision_attempt"] = vision_attempt
            if selected_channel is not None and hasattr(provider, "_execute_sticky"):
                response = provider._execute_sticky(
                    selected_channel,
                    "analyze",
                    boundary=self.process_boundary,
                    **call,
                )
            elif self.process_boundary is not None and hasattr(provider, "analyze_isolated"):
                response = provider.analyze_isolated(self.process_boundary, **call)
            elif self.process_boundary is not None:
                specification = provider.process_spec()
                if specification is None:
                    self.process_boundary.record_cooperative()
                    response = provider.analyze(
                        image_path=image,
                        model=model,
                        detail=detail,
                        stage=stage,
                        max_tokens=max_tokens,
                        reasoning_effort=reasoning_effort,
                        caption_controls=caption_controls,
                        vision_attempt=vision_attempt,
                        provider_request_context_id=provider_request_context_id,
                    )
                else:
                    response = self.process_boundary.call_provider(
                        specification,
                        "analyze",
                        timeout_seconds=float(getattr(provider, "timeout", 120)),
                        kwargs=call,
                    )
            else:
                response = provider.analyze(
                    image_path=image,
                    model=model,
                    detail=detail,
                    stage=stage,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    caption_controls=caption_controls,
                    vision_attempt=vision_attempt,
                    provider_request_context_id=provider_request_context_id,
                )
            vision_attempt.vision_started = True
            vision_attempt.vision_completed = True
        except TimeoutError as exc:
            if attempt_id is not None:
                self._trace_write(
                    "finish_attempt",
                    attempt_id,
                    status="TIMEOUT",
                    result="TIMEOUT",
                    request_json=sanitize_ai_payload(
                        getattr(exc, "trace_request_json", None), photo=photo_metadata
                    ),
                    response_raw=getattr(exc, "trace_response_raw", None),
                    request_built_at=getattr(exc, "trace_request_built_at", None),
                    response_received_at=getattr(exc, "trace_response_received_at", None),
                    endpoint=getattr(exc, "trace_endpoint", None),
                    api_mode="chat_completions",
                    http_status=getattr(exc, "trace_http_status", None),
                    latency_ms=int((time.perf_counter() - started_perf) * 1000),
                    error_code=str(getattr(exc, "code", "AI-PROVIDER-TIMEOUT")),
                    error_message=str(exc),
                    retry_delay_ms=self._trace_retry_delay_ms(exc),
                )
            self._trace_write(
                "mark_trace",
                trace_id,
                status="TIMEOUT",
                error_code=str(getattr(exc, "code", "AI-PROVIDER-TIMEOUT")),
                error_message=str(exc),
            )
            self._activity(
                "WARNING",
                "provider_timeout",
                "Caption Provider 請求逾時",
                job_id=job_id,
                photo_id=photo_id,
                stage=stage,
                error_code="AI-PROVIDER-TIMEOUT",
            )
            raise
        except Exception as exc:
            error_code = str(getattr(exc, "code", "AI-PROVIDER-UNAVAILABLE"))
            if attempt_id is not None:
                self._trace_write(
                    "finish_attempt",
                    attempt_id,
                    status="FAILED",
                    result="FAILED",
                    request_json=sanitize_ai_payload(
                        getattr(exc, "trace_request_json", None), photo=photo_metadata
                    ),
                    response_raw=getattr(exc, "trace_response_raw", None),
                    request_built_at=getattr(exc, "trace_request_built_at", None),
                    response_received_at=getattr(exc, "trace_response_received_at", None),
                    endpoint=getattr(exc, "trace_endpoint", None),
                    api_mode="chat_completions",
                    http_status=getattr(exc, "trace_http_status", getattr(exc, "http_status", None)),
                    latency_ms=int((time.perf_counter() - started_perf) * 1000),
                    error_code=error_code,
                    error_message=str(exc),
                    retry_delay_ms=self._trace_retry_delay_ms(exc),
                )
            self._trace_write(
                "mark_trace",
                trace_id,
                status="FAILED",
                error_code=error_code,
                error_message=str(exc),
            )
            self._activity(
                "ERROR",
                "provider_request_failed",
                "Caption Provider 請求失敗",
                job_id=job_id,
                photo_id=photo_id,
                stage=stage,
                error_code=error_code,
            )
            raise
        total_cost, usage_id = self._record(
            concrete_provider, model, job_id, photo_id, stage, response, started_at, started_perf
        )
        request_payload = sanitize_ai_payload(response.request_json_sanitized, photo=photo_metadata)
        response_received_at = response.response_received_at or datetime.now(timezone.utc).isoformat()
        trace_response_received_at = response_received_at
        self._trace_write(
            "add_event",
            trace_id,
            "RESPONSE_PARSE_STARTED",
            attempt_id=attempt_id,
        )
        total_input_tokens = response.usage.input_tokens
        total_output_tokens = response.usage.output_tokens
        total_cached_tokens = response.usage.cached_tokens
        total_reasoning_tokens = response.usage.reasoning_tokens
        try:
            local_json = extract_json_value(response.content)
            candidate = local_json if isinstance(local_json, dict) else response.content
            result = self._apply_caption_variant(validate_analysis_result(candidate), caption_controls)
            raw = response.content
            if attempt_id is not None:
                self._trace_write(
                    "finish_attempt",
                    attempt_id,
                    status="SUCCESS",
                    result="VALIDATED",
                    request_json=request_payload,
                    response_raw=response.raw_response or response.content,
                    response_parsed=result,
                    request_built_at=response.request_built_at,
                    response_received_at=response_received_at,
                    endpoint=response.endpoint,
                    api_mode="chat_completions",
                    http_status=response.http_status,
                    latency_ms=int((time.perf_counter() - started_perf) * 1000),
                    provider_request_id=response.request_id,
                    api_usage_id=usage_id,
                )
            self._trace_write(
                "add_event",
                trace_id,
                "RESPONSE_PARSED",
                attempt_id=attempt_id,
                created_at=response_received_at,
            )
            self._trace_write("add_event", trace_id, "SCHEMA_VALIDATED", attempt_id=attempt_id)
            if caption_controls and caption_controls["caption_variants_enabled"]:
                self._activity(
                    "DEBUG",
                    "caption_variants_generated",
                    "Caption 多風格候選已由單次圖片請求產生",
                    job_id=job_id,
                    photo_id=photo_id,
                    stage=stage,
                    trace_id=trace_id,
                )
        except AnalysisValidationError as first_error:
            if attempt_id is not None:
                self._trace_write(
                    "finish_attempt",
                    attempt_id,
                    status="FAILED",
                    result="INVALID_RESPONSE",
                    request_json=request_payload,
                    response_raw=response.raw_response or response.content,
                    request_built_at=response.request_built_at,
                    response_received_at=response_received_at,
                    endpoint=response.endpoint,
                    api_mode="chat_completions",
                    http_status=response.http_status,
                    latency_ms=int((time.perf_counter() - started_perf) * 1000),
                    provider_request_id=response.request_id,
                    api_usage_id=usage_id,
                    error_code="SCHEMA_VALIDATION_FAILED",
                    error_message=str(first_error),
                )
            vision_attempt.repair_attempted = True
            self._activity(
                "DEBUG",
                "provider_json_retry",
                "Caption Provider JSON 修復重試",
                job_id=job_id,
                photo_id=photo_id,
                stage=stage,
                trace_id=trace_id,
            )
            repair_started_at = datetime.now(timezone.utc).isoformat()
            repair_perf = time.perf_counter()
            frozen_repair_policy = dict(repair_policy or {})
            if not bool(frozen_repair_policy.get("enabled", True)):
                self._trace_write(
                    "mark_trace",
                    trace_id,
                    status="FAILED",
                    error_code="SCHEMA_VALIDATION_FAILED",
                    error_message=str(first_error),
                )
                raise first_error
            repair_model = str(frozen_repair_policy.get("model") or model).strip() or model
            try:
                repair_cap = int(frozen_repair_policy.get("max_tokens", REPAIR_TOKEN_CAP))
            except (TypeError, ValueError):
                repair_cap = REPAIR_TOKEN_CAP
            repair_attempt_id = self._trace_write(
                "start_attempt",
                trace_id=trace_id,
                provider=provider_identity,
                model=repair_model,
                started_at=repair_started_at,
                retry_reason="schema_validation_failed",
            )
            repair_call = {
                "invalid_content": response.content,
                "validation_error": str(first_error),
                "model": repair_model,
                "max_tokens": max(256, min(repair_cap, REPAIR_TOKEN_CAP)),
                "stage": stage,
                "caption_controls": caption_controls,
                "provider_request_context_id": provider_request_context_id,
            }
            try:
                if selected_channel is not None and hasattr(provider, "_execute_sticky"):
                    repaired = provider._execute_sticky(
                        selected_channel,
                        "repair_json",
                        boundary=self.process_boundary,
                        **repair_call,
                    )
                elif self.process_boundary is not None and hasattr(provider, "repair_json_isolated"):
                    repaired = provider.repair_json_isolated(self.process_boundary, **repair_call)
                elif self.process_boundary is not None:
                    specification = provider.process_spec()
                    if specification is None:
                        self.process_boundary.record_cooperative()
                        repaired = provider.repair_json(**repair_call)
                    else:
                        repaired = self.process_boundary.call_provider(
                            specification,
                            "repair_json",
                            timeout_seconds=float(getattr(provider, "timeout", 120)),
                            kwargs=repair_call,
                        )
                else:
                    repaired = provider.repair_json(**repair_call)
            except Exception as exc:
                repair_status = "TIMEOUT" if isinstance(exc, TimeoutError) else "FAILED"
                repair_error_code = str(
                    getattr(
                        exc,
                        "code",
                        "AI-PROVIDER-TIMEOUT"
                        if repair_status == "TIMEOUT"
                        else "AI-PROVIDER-UNAVAILABLE",
                    )
                )
                if repair_attempt_id is not None:
                    self._trace_write(
                        "finish_attempt",
                        repair_attempt_id,
                        status=repair_status,
                        result="REPAIR_FAILED",
                        request_json=sanitize_ai_payload(
                            getattr(exc, "trace_request_json", None), photo=photo_metadata
                        ),
                        response_raw=getattr(exc, "trace_response_raw", None),
                        request_built_at=getattr(exc, "trace_request_built_at", None),
                        response_received_at=getattr(exc, "trace_response_received_at", None),
                        endpoint=getattr(exc, "trace_endpoint", None),
                        api_mode="chat_completions",
                        http_status=getattr(exc, "trace_http_status", getattr(exc, "http_status", None)),
                        latency_ms=int((time.perf_counter() - repair_perf) * 1000),
                        error_code=repair_error_code,
                        error_message=str(exc),
                        retry_delay_ms=self._trace_retry_delay_ms(exc),
                    )
                self._trace_write(
                    "mark_trace",
                    trace_id,
                    status=repair_status,
                    error_code=repair_error_code,
                    error_message=str(exc),
                )
                raise
            repair_cost, repair_usage_id = self._record(
                concrete_provider,
                repair_model,
                job_id,
                photo_id,
                "json_repair",
                repaired,
                repair_started_at,
                repair_perf,
                retry_count=1,
            )
            total_cost += repair_cost
            self._trace_write(
                "add_event",
                trace_id,
                "RESPONSE_PARSE_STARTED",
                attempt_id=repair_attempt_id,
            )
            try:
                # 第二次驗證失敗直接拋出；不得無限修復。
                result = self._apply_caption_variant(
                    validate_analysis_result(repaired.content), caption_controls
                )
            except AnalysisValidationError as repair_error:
                if repair_attempt_id is not None:
                    self._trace_write(
                        "finish_attempt",
                        repair_attempt_id,
                        status="FAILED",
                        result="INVALID_RESPONSE",
                        request_json=sanitize_ai_payload(
                            repaired.request_json_sanitized, photo=photo_metadata
                        ),
                        response_raw=repaired.raw_response or repaired.content,
                        request_built_at=repaired.request_built_at,
                        response_received_at=repaired.response_received_at,
                        endpoint=repaired.endpoint,
                        api_mode="chat_completions",
                        http_status=repaired.http_status,
                        latency_ms=int((time.perf_counter() - repair_perf) * 1000),
                        provider_request_id=repaired.request_id,
                        api_usage_id=repair_usage_id,
                        error_code="SCHEMA_VALIDATION_FAILED",
                        error_message=str(repair_error),
                    )
                self._trace_write(
                    "mark_trace",
                    trace_id,
                    status="FAILED",
                    error_code="SCHEMA_VALIDATION_FAILED",
                    error_message=str(repair_error),
                )
                raise
            raw = repaired.content
            repair_received_at = repaired.response_received_at or datetime.now(timezone.utc).isoformat()
            trace_response_received_at = repair_received_at
            if repair_attempt_id is not None:
                self._trace_write(
                    "finish_attempt",
                    repair_attempt_id,
                    status="SUCCESS",
                    result="VALIDATED",
                    request_json=sanitize_ai_payload(repaired.request_json_sanitized, photo=photo_metadata),
                    response_raw=repaired.raw_response or repaired.content,
                    response_parsed=result,
                    request_built_at=repaired.request_built_at,
                    response_received_at=repair_received_at,
                    endpoint=repaired.endpoint,
                    api_mode="chat_completions",
                    http_status=repaired.http_status,
                    latency_ms=int((time.perf_counter() - repair_perf) * 1000),
                    provider_request_id=repaired.request_id,
                    api_usage_id=repair_usage_id,
                )
            self._trace_write(
                "add_event",
                trace_id,
                "RESPONSE_PARSED",
                attempt_id=repair_attempt_id,
                created_at=repair_received_at,
            )
            self._trace_write("add_event", trace_id, "SCHEMA_VALIDATED", attempt_id=repair_attempt_id)
            total_input_tokens += repaired.usage.input_tokens
            total_output_tokens += repaired.usage.output_tokens
            total_cached_tokens += repaired.usage.cached_tokens
            total_reasoning_tokens += repaired.usage.reasoning_tokens
        self._trace_write(
            "mark_trace",
            trace_id,
            status="RUNNING",
            response_received_at=trace_response_received_at,
        )
        self.photos.put_ai_cache(
            content_sha256=content_sha256,
            provider=cache_provider_identity,
            model_name=model,
            prompt_version=prompt_version,
            # A frozen v3 plan owns a v3 cache identity even when a
            # compatibility provider still returns the accepted v2 payload.
            # Legacy plans retain the result's historical schema version.
            schema_version=int(cache_schema_version or result["schema_version"]),
            schema_kind=cache_schema_kind,
            result=result,
            raw_json=raw,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cached_tokens=total_cached_tokens,
            estimated_cost=total_cost,
            latency_ms=int((time.perf_counter() - started_perf) * 1000),
            vision_request_fingerprint=vision_request_fingerprint,
            vision_input_spec_json=vision_input_spec_json,
        )
        latency = int((time.perf_counter() - started_perf) * 1000)
        return (
            result,
            raw,
            total_cost,
            Usage(
                total_input_tokens,
                total_output_tokens,
                total_cached_tokens,
                total_reasoning_tokens,
            ),
            latency,
        )

    def analyze_photo(
        self,
        *,
        photo_id: str,
        job_id: str | None,
        provider: VisionProvider | None,
        strategy: str,
        low_model: str = "low-cost-vision",
        high_model: str = "high-quality-vision",
        stage_two_threshold: float = 65,
        favorite_override: bool = True,
        ranking_weights: dict[str, float] | None = None,
        favorite_bonus: float = DEFAULT_FAVORITE_BONUS,
        scoring_version_id: str | None = None,
        force_ai: bool = False,
        force_actor: str = "system",
        force_recompute: bool = False,
        analysis_plan: dict | None = None,
    ) -> dict:
        photo = self.photos.get_with_path(photo_id)
        if photo is None:
            raise FileNotFoundError("SCAN-001 找不到照片資料")
        source = safe_join(Path(photo["root_path"]), str(photo["relative_path"]))
        if not source.is_file():
            raise FileNotFoundError("SCAN-001 找不到照片檔案")
        photo = self._ensure_e6_suitability(photo_id, photo, source)
        analysis_spec = dict(analysis_plan or {})
        if not analysis_spec:
            analysis_spec = build_analysis_plan(
                strategy=strategy,
                provider_route=[],
                low_model=low_model,
                high_model=high_model,
                stage_two_threshold=stage_two_threshold,
                favorite_override=favorite_override,
                scoring_profile={
                    "id": scoring_version_id or "",
                    "memory_weight": (ranking_weights or DEFAULT_RANKING_WEIGHTS)["memory"],
                    "beauty_weight": (ranking_weights or DEFAULT_RANKING_WEIGHTS)["beauty"],
                    "technical_weight": (ranking_weights or DEFAULT_RANKING_WEIGHTS)["technical_quality"],
                    "emotion_weight": (ranking_weights or DEFAULT_RANKING_WEIGHTS)["emotion"],
                    "favorite_bonus": favorite_bonus,
                },
                caption_controls=self._caption_generation_controls(self._caption_controls()),
                prompt_version=self._prompt_version(
                    self._caption_generation_controls(self._caption_controls())
                ),
                high_image_max_side=int(
                    self.settings.get(
                        "analysis.image_max_side", self.settings.get("analysis.high_image_max_side", 1024)
                    )
                )
                if self.settings
                else 1024,
                caption_display_controls=self._caption_display_controls(self._caption_controls()),
                repair_policy={
                    "enabled": True,
                    "model": str(
                        self.settings.get("model.repair_model", high_model) if self.settings else high_model
                    ),
                    "max_tokens": max(
                        256,
                        min(
                            REPAIR_TOKEN_CAP,
                            int(
                                self.settings.get("budget.repair_max_tokens", REPAIR_TOKEN_CAP)
                                if self.settings
                                else REPAIR_TOKEN_CAP
                            ),
                        ),
                    ),
                    "max_attempts": 1,
                    "text_only": True,
                },
            )
        analysis_spec = normalize_analysis_plan(analysis_spec)
        analysis_spec["reasoning_effort"] = normalize_reasoning_effort(
            analysis_spec.get("reasoning_effort", "none")
        )
        strategy = str(analysis_spec["strategy"])
        model = str(analysis_spec.get("model") or analysis_spec.get("high_model") or "")
        favorite_override = bool(analysis_spec["favorite_override"])
        caption_controls = dict(analysis_spec.get("caption_controls") or {}) or None
        display_controls = dict(analysis_spec.get("caption_display_controls") or {})
        repair_policy = dict(analysis_spec.get("repair_policy") or {})
        if caption_controls:
            # Display style is frozen for the job but excluded from the
            # provider prompt and Vision request identity.
            caption_controls = dict(caption_controls) | display_controls
        prompt_version = str(analysis_spec["prompt_version"])
        vision_input = dict(analysis_spec["vision_input"])
        image_max_side = int(vision_input["max_side"])
        ranking_weights = dict(analysis_spec["ranking_weights"])
        favorite_bonus = float(analysis_spec["favorite_bonus"])
        scoring_version_id = str(analysis_spec["scoring_profile_id"]) or scoring_version_id
        analysis_spec_json = canonical_json(analysis_spec)
        identity_spec = dict(analysis_spec)
        identity_spec.pop("caption_display_controls", None)
        identity_spec.pop("repair_policy", None)
        analysis_fingerprint = fingerprint(identity_spec)
        content_sha = str(photo["sha256"] or "")
        plan_prefilter = {
            "analysis.prefilter_enabled": bool(analysis_spec.get("prefilter", {}).get("enabled", True)),
            "analysis.prefilter_screenshots": bool(
                analysis_spec.get("prefilter", {}).get("screenshots_enabled", True)
            ),
            "analysis.prefilter_low_quality": bool(
                analysis_spec.get("prefilter", {}).get("low_quality_enabled", True)
            ),
            "analysis.prefilter_sensitivity": str(
                analysis_spec.get("prefilter", {}).get("sensitivity", "conservative")
            ),
            "analysis.e6_prefilter_enabled": bool(analysis_spec.get("prefilter", {}).get("e6_enabled", True)),
            "analysis.e6_min_score": float(analysis_spec.get("prefilter", {}).get("e6_min_score", 25)),
        }
        execution_policy = dict(analysis_spec.get("ai_execution_policy") or {})
        travel_policy = dict(analysis_spec.get("travel_policy") or {})
        if str(execution_policy.get("execution_mode", "automatic_ai")) == "disabled":
            raise AnalysisDisabledError("目前分析執行模式為完全停用，不會建立新的分析結果")

        def local_context(schema_kind: str) -> dict[str, Any]:
            input_spec = {
                "mode": "local-only",
                "schema_kind": schema_kind,
                "feature_version": FEATURE_VERSION,
                "analysis_plan_fingerprint": analysis_fingerprint,
            }
            input_json = canonical_json(input_spec)
            return {
                "prompt_version": prompt_version,
                "analysis_fingerprint": analysis_fingerprint,
                "analysis_spec_json": analysis_spec_json,
                "vision_request_fingerprint": fingerprint(
                    {
                        "content_sha256": content_sha,
                        "provider_id": "local",
                        "prompt_version": prompt_version,
                        **input_spec,
                    }
                ),
                "vision_input_spec_json": input_json,
                "travel_policy": travel_policy,
            }

        self._activity(
            "DEBUG",
            "caption_analysis_started",
            "Caption 分析開始",
            job_id=job_id,
            photo_id=photo_id,
            stage=strategy,
            trace_id=prompt_version,
            advanced_caption=bool(caption_controls),
        )
        weights = ranking_weights or DEFAULT_RANKING_WEIGHTS
        # Identical bytes can only inherit an analysis that was produced by the
        # exact same frozen plan.  Reusing a different prompt/schema/Vision
        # Input would make selection-preview incorrectly consider it current.
        inherited = self.photos.inherit_existing_analysis(
            photo_id,
            job_id,
            analysis_context={"analysis_fingerprint": analysis_fingerprint},
        )
        if inherited is not None:
            return {"analysis": inherited, "stage": "inherited", "_actual_cost": 0}
        if strategy == "local":
            result = validate_analysis_result(self._local_result(photo))
            raw = json.dumps(result, ensure_ascii=False)
            result = self._save_result(
                photo_id=photo_id,
                job_id=job_id,
                stage="local",
                provider="local",
                model="local",
                result=result,
                raw=raw,
                photo=photo,
                ranking_weights=weights,
                favorite_bonus=favorite_bonus,
                scoring_version_id=scoring_version_id,
                schema_kind="basic",
                **local_context("basic"),
            )
            return {"analysis": result, "stage": "local", "_actual_cost": 0}

        if (
            self.settings is not None
            and not force_ai
            and not bool(photo["eligible"])
            and not bool(photo["manual_override"])
        ):
            result = validate_analysis_result(
                self._prefilter_result(photo, policy_settings=plan_prefilter) or self._local_result(photo)
            )
            result["should_keep"] = False
            raw = json.dumps(result, ensure_ascii=False)
            result = self._save_result(
                photo_id=photo_id,
                job_id=job_id,
                stage="prefilter",
                provider="local",
                model=FEATURE_VERSION,
                result=result,
                raw=raw,
                photo=photo,
                ranking_weights=weights,
                favorite_bonus=favorite_bonus,
                scoring_version_id=scoring_version_id,
                schema_kind="basic",
                **local_context("basic"),
            )
            return {"analysis": result, "stage": "prefilter", "_actual_cost": 0}

        prefiltered = (
            None
            if force_ai or self.settings is None
            else self._prefilter_result(photo, policy_settings=plan_prefilter)
        )
        if prefiltered is not None:
            prefilter_evaluation = self.prefilter_snapshot(photo, policy_settings=plan_prefilter)
            result = validate_analysis_result(prefiltered)
            raw = json.dumps(result, ensure_ascii=False)
            result = self._save_result(
                photo_id=photo_id,
                job_id=job_id,
                stage="prefilter",
                provider="local",
                model="local-prefilter",
                result=result,
                raw=raw,
                photo=photo,
                ranking_weights=weights,
                favorite_bonus=favorite_bonus,
                scoring_version_id=scoring_version_id,
                schema_kind="basic",
                **local_context("basic"),
                prefilter_evaluation=prefilter_evaluation,
            )
            return {"analysis": result, "stage": "prefilter", "_actual_cost": 0}

        if not self._allow_ai_for_photo(photo_id, force_ai=force_ai, execution_policy=execution_policy) or (
            not force_ai and self._photo_limits_reached(execution_policy)
        ):
            result = validate_analysis_result(self._local_result(photo))
            raw = json.dumps(result, ensure_ascii=False)
            result = self._save_result(
                photo_id=photo_id,
                job_id=job_id,
                stage="local_fallback",
                provider="local",
                model="local-quality-v3",
                result=result,
                raw=raw,
                photo=photo,
                ranking_weights=weights,
                favorite_bonus=favorite_bonus,
                scoring_version_id=scoring_version_id,
                schema_kind="basic",
                **local_context("basic"),
            )
            return {"analysis": result, "stage": "local_fallback", "_actual_cost": 0}
        if provider is None:
            raise ProviderUnavailableError("VLM-008 尚未設定可用 Provider")

        sha = str(photo["sha256"] or "")
        if not sha:
            raise ValueError("IMG-003 照片尚未完成本地預處理")
        total_cost = 0.0

        def record_force(provider_name: str, model_name: str) -> None:
            if force_ai:
                self.photos.record_force_ai_event(
                    photo_id,
                    job_id=job_id,
                    provider=provider_name,
                    provider_name=str(getattr(provider, "name", provider_name)),
                    model=model_name,
                    actor=force_actor,
                )

        (
            result,
            raw,
            cost,
            cache_hit,
            actual_provider,
            actual_model,
            request_fingerprint,
            input_spec_json,
            _usage,
            _latency,
            ai_trace_id,
        ) = self._model_call(
            provider=provider,
            image_factory=lambda: self.thumbnails.acquire_for_use(source, sha, image_max_side),
            model=model,
            detail=str(vision_input.get("detail", "high")),
            stage="single",
            job_id=job_id,
            photo_id=photo_id,
            content_sha256=sha,
            schema_kind="full",
            reasoning_effort=str(analysis_spec["reasoning_effort"]),
            caption_controls=caption_controls,
            repair_policy=repair_policy,
            prompt_version=prompt_version,
            vision_input=vision_input,
            analysis_fingerprint=analysis_fingerprint,
            photo_metadata={
                "id": photo_id,
                "sha256": sha,
                "width": photo["width"],
                "height": photo["height"],
                "mime_type": {
                    "JPEG": "image/jpeg",
                    "JPG": "image/jpeg",
                    "PNG": "image/png",
                    "WEBP": "image/webp",
                    "HEIC": "image/heic",
                }.get(str(photo["format"] or "").upper()),
            },
            provider_prompt_contract_sha256=str(analysis_spec.get("provider_prompt_contract_sha256") or "") or None,
            force_recompute=force_recompute,
        )
        total_cost = cost
        try:
            result = self._save_result(
                photo_id=photo_id,
                job_id=job_id,
                stage="single",
                provider=actual_provider,
                model=actual_model,
                result=result,
                raw=raw,
                photo=photo,
                ranking_weights=weights,
                favorite_bonus=favorite_bonus,
                scoring_version_id=scoring_version_id,
                schema_kind="full",
                prompt_version=prompt_version,
                analysis_fingerprint=analysis_fingerprint,
                analysis_spec_json=analysis_spec_json,
                vision_request_fingerprint=request_fingerprint,
                vision_input_spec_json=input_spec_json,
                travel_policy=travel_policy,
                trace_id=ai_trace_id,
            )
        except Exception as exc:
            if ai_trace_id:
                self._trace_write(
                    "mark_trace",
                    ai_trace_id,
                    status="FAILED",
                    error_code="RESULT_PERSIST_FAILED",
                    error_message=str(exc),
                )
            raise
        if ai_trace_id:
            self._trace_write("persist_final_result", ai_trace_id, result)
        record_force(actual_provider, actual_model)
        return {
            "analysis": result,
            "stage": "cache" if cache_hit else "single",
            "_actual_cost": total_cost,
        }
