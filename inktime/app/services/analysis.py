from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
from typing import Any, Callable

from PIL import Image, ImageOps

from inktime.app.core.paths import safe_join
from inktime.app.domain.analysis import (
    AnalysisValidationError,
    build_analysis_plan,
    canonical_json,
    fingerprint,
    validate_analysis_result,
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
from inktime.app.domain.rendering import evaluate_e6_suitability
from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.budgets import BudgetService


PROMPT_VERSION = "photo-quality-v4-visual-orientation"


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
    ) -> None:
        self.photos = photos
        self.usage = usage
        self.thumbnails = thumbnails
        self.budgets = budgets
        self.settings = settings or (budgets.settings if budgets else None)
        self.observability = observability
        self.process_boundary = process_boundary

    def _activity(self, severity: str, event: str, message: str, **fields) -> None:
        if self.observability is not None:
            self.observability.record(severity, "analysis", event, message, **fields)

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
            "copy_avoid_direct_description": bool(settings.get("analysis.copy_avoid_direct_description", True)),
            "copy_forbid_exclamation": bool(settings.get("analysis.copy_forbid_exclamation", True)),
            "copy_forbid_like_phrase": bool(settings.get("analysis.copy_forbid_like_phrase", True)),
            "copy_max_commas": int(settings.get("analysis.copy_max_commas", 2)),
            "copy_avoid_abstract_ending": bool(settings.get("analysis.copy_avoid_abstract_ending", True)),
            "copy_banned_words": lines("analysis.copy_banned_words"),
            "copy_banned_patterns": lines("analysis.copy_banned_patterns"),
            "copy_custom_rules": str(self.settings.get("analysis.copy_custom_rules", "")),
            "caption_variants_enabled": bool(self.settings.get("analysis.caption_variants_enabled", False)),
        }

    def build_plan(self, *, strategy: str, provider_route: list[dict], scoring_profile: dict) -> dict:
        """Build the sole server-authoritative non-secret Analysis Plan."""
        if self.settings is None:
            raise RuntimeError("分析設定尚未初始化")
        settings = self.settings
        controls = self._caption_controls()
        prompt_version = self._prompt_version(controls)
        return build_analysis_plan(
            strategy=strategy,
            provider_route=provider_route,
            low_model=str(settings.get("model.low_model", "low-cost-vision")),
            high_model=str(settings.get("model.high_model", "high-quality-vision")),
            stage_two_threshold=float(settings.get("analysis.stage_two_threshold", 65)),
            favorite_override=bool(settings.get("analysis.favorite_override", True)),
            scoring_profile=scoring_profile,
            caption_controls=controls,
            prompt_version=prompt_version,
            high_image_max_side=int(settings.get("analysis.high_image_max_side", 1024)),
        )

    @staticmethod
    def _prompt_version(caption_controls: dict | None) -> str:
        if not caption_controls:
            return PROMPT_VERSION
        fingerprint = hashlib.sha256(json.dumps(caption_controls, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
        return f"{PROMPT_VERSION}-caption-{fingerprint}"

    @staticmethod
    def _apply_caption_variant(result: dict, caption_controls: dict | None) -> dict:
        if not caption_controls or not caption_controls["caption_variants_enabled"]:
            return result
        variants = (result.get("details") or {}).get("caption_variants") or {}
        style = str(caption_controls["copy_default_style"])
        selected = variants.get(style) or variants.get("natural") or result.get("side_caption") or "畫面把此刻收好了。"
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

    def prefilter_snapshot(self, photo) -> dict:
        policy_settings = {
            key: self.settings.get(key, default) if self.settings is not None else default
            for key, default in (
                ("analysis.prefilter_enabled", True), ("analysis.prefilter_screenshots", True),
                ("analysis.prefilter_low_quality", True), ("analysis.prefilter_sensitivity", "conservative"),
                ("analysis.e6_prefilter_enabled", True), ("analysis.e6_min_score", 25),
            )
        }
        policy = evaluate_local_quality(dict(photo), settings=policy_settings)
        labels = {"screenshot_strong": "明確截圖證據", "screenshot_score": "截圖信號分數",
                  "screenshot_independent_signals": "截圖獨立信號", "document_token_with_evidence": "文件或掃描證據",
                  "severe_blur": "嚴重模糊或失焦", "suspected_blur": "疑似模糊",
                  "short_edge_under_240": "解析度過低（短邊）", "tiny_empty": "極小且近乎空白",
                  "small_compressed": "小型壓縮檔", "extreme_exposure_low_contrast": "極端曝光且低對比",
                  "exposure_low_priority": "曝光比例偏高", "social_export": "社群平台轉存"}
        checks = [{"key": key, "label": labels.get(key, key), "hit": bool(hit)}
                  for key, hit in policy["evidence"]["checks"].items()]
        return {
            "enabled": bool(policy_settings["analysis.prefilter_enabled"]), "sensitivity": policy["sensitivity"],
            "feature_version": policy["feature_version"], "decision": policy["decision"],
            "excluded": policy["decision"] == "auto_excluded", "primary_reason": policy["primary_reason"],
            "matched_checks": policy["matched_checks"], "thresholds": policy["thresholds"],
            "e6_threshold": policy["e6_threshold"], "e6_feature_version": policy["e6_feature_version"],
            "evidence": policy["evidence"], "checks": checks,
            "summary": f"本機品質規則：{policy['decision']}（{policy['primary_reason']}）",
        }
    def _prefilter_result(self, photo) -> dict | None:
        evaluation = self.prefilter_snapshot(photo)
        if not evaluation["excluded"]:
            return None

        quality = self._local_quality(photo)
        if evaluation["decision"] == "excluded_screenshot":
            label = "截圖"
            reasons = ["本機截圖特徵達排除門檻"]
            memory_score = 5.0
            types = ["截圖"]
        elif evaluation["decision"] == "excluded_e6":
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
        if photo["e6_score"] is not None:
            return photo
        with Image.open(source) as opened:
            opened.draft("RGB", (256, 256))
            opened.thumbnail((256, 256), Image.Resampling.LANCZOS)
            metrics = evaluate_e6_suitability(ImageOps.exif_transpose(opened).convert("RGB"))
        self.photos.update_e6_suitability(photo_id, metrics)
        return self.photos.get_with_path(photo_id)

    def _ai_mode(self) -> str:
        return str(self.settings.get("analysis.ai_mode", "top_candidates")) if self.settings else "legacy"

    def _allow_ai_for_photo(self, photo_id: str, *, force_ai: bool) -> bool:
        mode = self._ai_mode()
        if mode == "off":
            return False
        if force_ai:
            return True
        if mode == "on_demand":
            return False
        if mode == "top_candidates":
            limit = int(self.settings.get("analysis.ai_top_n", 50)) if self.settings else 50
            return self.photos.is_top_candidate(photo_id, limit)
        return mode in {"eligible", "full_library", "legacy"}

    def _photo_limits_reached(self) -> bool:
        if self.settings is None:
            return False
        return self.photos.ai_limit_reached(
            daily_limit=int(self.settings.get("analysis.ai_daily_photo_limit", 50)),
            monthly_limit=int(self.settings.get("analysis.ai_monthly_photo_limit", 500)),
        )

    def _score_result(
        self,
        result: dict,
        photo,
        *,
        ranking_weights: dict[str, float],
        favorite_bonus: float,
    ) -> dict:
        details = result.get("details") or {}
        for target, grade_key in (
            ("memory_score", "memory_grade"),
            ("beauty_score", "aesthetic_grade"),
            ("technical_quality_score", "technical_grade"),
            ("emotion_score", "emotion_grade"),
        ):
            result[target] = grade_to_score(details.get(grade_key), float(result[target]))
        base = calculate_ranking_score(
            result,
            ranking_weights,
            favorite=bool(photo["favorite"]),
            favorite_bonus=favorite_bonus,
        )
        travel_bonus = 0.0
        location_rule_version = None
        if self.settings is not None and bool(self.settings.get("travel_bonus_enabled", True)):
            country = str(details.get("country_candidate") or "").strip().casefold()
            foreign = bool(country) and country not in {"tw", "taiwan", "台灣", "臺灣", "中華民國"}
            visits = self.photos.location_visit_count(photo["gps_lat"], photo["gps_lon"])
            travel_bonus, _distance = calculate_travel_bonus(
                latitude=photo["gps_lat"],
                longitude=photo["gps_lon"],
                home_latitude=self.settings.get("home_latitude"),
                home_longitude=self.settings.get("home_longitude"),
                home_radius_km=float(self.settings.get("home_radius_km", 60)),
                near_bonus=float(self.settings.get("travel_bonus_near", 2)),
                far_bonus=float(self.settings.get("travel_bonus_far", 4)),
                foreign_bonus=float(self.settings.get("foreign_country_bonus", 6)),
                rare_bonus=float(self.settings.get("rare_location_bonus", 2)),
                foreign_country=foreign,
                rare_location=0 < visits <= 3,
                maximum=float(self.settings.get("max_total_bonus", 8)),
            )
            location_rule_version = str(self.settings.get("location_rule_version", "travel-v1"))
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
    ) -> dict:
        ranked = self._score_result(
            result, photo, ranking_weights=ranking_weights, favorite_bonus=favorite_bonus
        )
        self.photos.save_analysis(
            photo_id,
            job_id,
            stage,
            provider,
            model,
            ranked,
            raw,
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
        )
        self._activity("DEBUG", "caption_analysis_completed", "Caption 分析完成", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=prompt_version)
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
    ) -> float:
        cost = provider.estimate_cost(model, response.usage)
        self.usage.record(
            provider=provider.name,
            model=model,
            job_id=job_id,
            photo_id=photo_id,
            request_type=request_type,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_tokens=response.usage.cached_tokens,
            estimated_cost=cost,
            actual_cost=cost,
            started_at=started_at,
            latency_ms=int((time.perf_counter() - started_perf) * 1000),
            status="completed",
            retry_count=retry_count,
        )
        return cost

    def _model_call(
        self,
        *,
        provider: VisionProvider,
        image_factory: Callable[[], Path],
        model: str,
        detail: str,
        stage: str,
        job_id: str | None,
        photo_id: str,
        content_sha256: str,
        schema_kind: str,
        caption_controls: dict | None,
        prompt_version: str,
        vision_input: dict,
        force_recompute: bool = False,
        _excluded_providers: set[str] | None = None,
    ) -> tuple[dict, str, float, bool, str, str, str, str, Usage, int]:
        selected_channel = None
        selected_provider = provider
        selector = getattr(provider, "select_channel", None)
        if callable(selector):
            selected_channel = selector(excluded=_excluded_providers)
            selected_provider = selected_channel.provider
        actual_provider = selected_provider.name

        def release_selected(*, usage: Usage | None = None, error: Exception | None = None) -> None:
            if selected_channel is not None:
                router: Any = provider
                router.release_channel(selected_channel, usage=usage, error=error)
        request_fingerprint = fingerprint({
            "content_sha256": content_sha256, "actual_provider": actual_provider,
            "model": model, "prompt_version": prompt_version, "schema_version": 2,
            "schema_kind": schema_kind, **vision_input,
        })
        # Legacy schema constrains this column to basic/full; the v4 Vision
        # Request Fingerprint is the authoritative additional cache dimension.
        cache_schema_kind = schema_kind
        vision_json = canonical_json(vision_input)
        if not force_recompute:
            cached = self.photos.get_ai_cache(
                content_sha256=content_sha256, provider=actual_provider, model_name=model,
                prompt_version=prompt_version, schema_version=2, schema_kind=cache_schema_kind,
                vision_request_fingerprint=request_fingerprint,
            )
            if cached is not None:
                try:
                    self._activity("DEBUG", "caption_cache_hit", "Caption AI Cache 命中", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=request_fingerprint)
                    release_selected()
                    return validate_analysis_result(cached["result"]), str(cached["raw_json"]), 0.0, True, actual_provider, model, request_fingerprint, vision_json, Usage(), 0
                except AnalysisValidationError:
                    pass
        cache_key = request_fingerprint
        self._activity("DEBUG", "caption_cache_miss", "Caption AI Cache 未命中", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=prompt_version)
        owner_id = str(uuid4())
        deadline = time.monotonic() + 120
        waited_for_owner = False
        while not self.photos.acquire_ai_cache_reservation(cache_key, owner_id):
            waited_for_owner = True
            if time.monotonic() >= deadline:
                release_selected(error=TimeoutError("AI-CACHE-001 等待相同分析結果逾時"))
                raise TimeoutError("AI-CACHE-001 等待相同分析結果逾時")
            time.sleep(0.05)
            # A forced request ignores a pre-existing entry, but once another
            # forced owner has completed its fresh result, all waiters share it.
            if not force_recompute or waited_for_owner:
                cached = self.photos.get_ai_cache(
                    content_sha256=content_sha256, provider=actual_provider, model_name=model,
                    prompt_version=prompt_version, schema_version=2, schema_kind=cache_schema_kind,
                    vision_request_fingerprint=request_fingerprint,
                )
                if cached is not None:
                    self._activity("DEBUG", "caption_cache_hit", "等待中的 Caption AI Cache 已完成", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=prompt_version)
                    release_selected()
                    return validate_analysis_result(cached["result"]), str(cached["raw_json"]), 0.0, True, actual_provider, model, request_fingerprint, vision_json, Usage(), 0
        if not force_recompute:
            cached = self.photos.get_ai_cache(
                content_sha256=content_sha256, provider=actual_provider, model_name=model,
                prompt_version=prompt_version, schema_version=2, schema_kind=cache_schema_kind,
                vision_request_fingerprint=request_fingerprint,
            )
            if cached is not None:
                self.photos.finish_ai_cache_reservation(cache_key, owner_id)
                release_selected()
                return validate_analysis_result(cached["result"]), str(cached["raw_json"]), 0.0, True, actual_provider, model, request_fingerprint, vision_json, Usage(), 0
        try:
            # The only owner generates a JPEG, after both cache checks.
            image = image_factory()
            result, raw, cost, usage, latency = self._perform_uncached_model_call(
                provider=selected_provider,
                image=image,
                model=model,
                detail=detail,
                stage=stage,
                job_id=job_id,
                photo_id=photo_id,
                content_sha256=content_sha256,
                schema_kind=schema_kind,
                caption_controls=caption_controls,
                prompt_version=prompt_version,
                cache_schema_kind=cache_schema_kind,
                vision_request_fingerprint=request_fingerprint,
                vision_input_spec_json=vision_json,
            )
        except Exception as exc:
            self.photos.finish_ai_cache_reservation(cache_key, owner_id, error=str(exc))
            release_selected(error=exc)
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
                    caption_controls=caption_controls,
                    prompt_version=prompt_version,
                    vision_input=vision_input,
                    force_recompute=force_recompute,
                    _excluded_providers=excluded,
                )
            raise
        self.photos.finish_ai_cache_reservation(cache_key, owner_id)
        release_selected(usage=usage)
        return result, raw, cost, False, actual_provider, model, request_fingerprint, vision_json, usage, latency

    def _perform_uncached_model_call(
        self,
        *,
        provider: VisionProvider,
        image: Path,
        model: str,
        detail: str,
        stage: str,
        job_id: str | None,
        photo_id: str,
        content_sha256: str,
        schema_kind: str,
        caption_controls: dict | None,
        prompt_version: str,
        cache_schema_kind: str,
        vision_request_fingerprint: str,
        vision_input_spec_json: str,
    ) -> tuple[dict, str, float, Usage, int]:
        if self.budgets:
            self.budgets.assert_request_allowed(job_id, photo_id)
        started_at = datetime.now(timezone.utc).isoformat()
        started_perf = time.perf_counter()
        max_tokens = int(self.budgets.settings.get("budget.max_tokens", 8000)) if self.budgets else None
        self._activity("DEBUG", "provider_request_started", "Caption Provider 請求開始", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=prompt_version, provider=provider.name, model=model)
        try:
            call = {
                "image_path": image,
                "model": model,
                "detail": detail,
                "stage": stage,
                "max_tokens": max_tokens,
                "caption_controls": caption_controls,
            }
            if self.process_boundary is not None and hasattr(provider, "analyze_isolated"):
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
                        caption_controls=caption_controls,
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
                    caption_controls=caption_controls,
                )
        except TimeoutError:
            self._activity("WARNING", "provider_timeout", "Caption Provider 請求逾時", job_id=job_id, photo_id=photo_id, stage=stage, error_code="AI-PROVIDER-TIMEOUT")
            raise
        except Exception:
            self._activity("ERROR", "provider_request_failed", "Caption Provider 請求失敗", job_id=job_id, photo_id=photo_id, stage=stage, error_code="AI-PROVIDER-UNAVAILABLE")
            raise
        total_cost = self._record(
            provider, model, job_id, photo_id, stage, response, started_at, started_perf
        )
        total_input_tokens = response.usage.input_tokens
        total_output_tokens = response.usage.output_tokens
        total_cached_tokens = response.usage.cached_tokens
        try:
            result = self._apply_caption_variant(validate_analysis_result(response.content), caption_controls)
            raw = response.content
            if caption_controls and caption_controls["caption_variants_enabled"]:
                self._activity("DEBUG", "caption_variants_generated", "Caption 多風格候選已由單次圖片請求產生", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=prompt_version)
        except AnalysisValidationError as first_error:
            self._activity("DEBUG", "provider_json_retry", "Caption Provider JSON 修復重試", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=prompt_version)
            repair_started_at = datetime.now(timezone.utc).isoformat()
            repair_perf = time.perf_counter()
            repair_call = {
                "invalid_content": response.content,
                "validation_error": str(first_error),
                "model": model,
                "max_tokens": max_tokens,
                "stage": stage,
                "caption_controls": caption_controls,
            }
            if self.process_boundary is not None and hasattr(provider, "repair_json_isolated"):
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
            total_cost += self._record(
                provider,
                model,
                job_id,
                photo_id,
                "json_repair",
                repaired,
                repair_started_at,
                repair_perf,
                retry_count=1,
            )
            # 第二次驗證失敗直接拋出；不得無限修復。
            result = self._apply_caption_variant(validate_analysis_result(repaired.content), caption_controls)
            raw = repaired.content
            total_input_tokens += repaired.usage.input_tokens
            total_output_tokens += repaired.usage.output_tokens
            total_cached_tokens += repaired.usage.cached_tokens
        self.photos.put_ai_cache(
            content_sha256=content_sha256,
            provider=provider.name,
            model_name=model,
            prompt_version=prompt_version,
            schema_version=int(result["schema_version"]),
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
        return result, raw, total_cost, Usage(total_input_tokens, total_output_tokens, total_cached_tokens), latency

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
                strategy=strategy, provider_route=[], low_model=low_model, high_model=high_model,
                stage_two_threshold=stage_two_threshold, favorite_override=favorite_override,
                scoring_profile={"id": scoring_version_id or "", "memory_weight": (ranking_weights or DEFAULT_RANKING_WEIGHTS)["memory"], "beauty_weight": (ranking_weights or DEFAULT_RANKING_WEIGHTS)["beauty"], "technical_weight": (ranking_weights or DEFAULT_RANKING_WEIGHTS)["technical_quality"], "emotion_weight": (ranking_weights or DEFAULT_RANKING_WEIGHTS)["emotion"], "favorite_bonus": favorite_bonus},
                caption_controls=self._caption_controls(), prompt_version=self._prompt_version(self._caption_controls()),
                high_image_max_side=int(self.settings.get("analysis.high_image_max_side", 1024)) if self.settings else 1024,
            )
        strategy = str(analysis_spec["strategy"])
        low_model = str(analysis_spec["low_model"])
        high_model = str(analysis_spec["high_model"])
        stage_two_threshold = float(analysis_spec["stage_two_threshold"])
        favorite_override = bool(analysis_spec["favorite_override"])
        caption_controls = dict(analysis_spec["caption_controls"]) or None
        prompt_version = str(analysis_spec["prompt_version"])
        high_max_side = int(analysis_spec["high_vision_input"]["max_side"])
        ranking_weights = dict(analysis_spec["ranking_weights"])
        favorite_bonus = float(analysis_spec["favorite_bonus"])
        scoring_version_id = str(analysis_spec["scoring_profile_id"]) or scoring_version_id
        analysis_spec_json = canonical_json(analysis_spec)
        analysis_fingerprint = fingerprint(analysis_spec)
        self._activity("DEBUG", "caption_analysis_started", "Caption 分析開始", job_id=job_id, photo_id=photo_id, stage=strategy, trace_id=prompt_version, advanced_caption=bool(caption_controls))
        weights = ranking_weights or DEFAULT_RANKING_WEIGHTS
        inherited = self.photos.inherit_existing_analysis(photo_id, job_id) if self.settings is None else None
        if inherited is not None:
            return {"analysis": inherited, "stage": "inherited", "_actual_cost": 0}
        if strategy == "local":
            result = validate_analysis_result(self._local_result(photo))
            raw = json.dumps(result, ensure_ascii=False)
            result = self._save_result(
                photo_id=photo_id, job_id=job_id, stage="local", provider="local", model="local",
                result=result, raw=raw, photo=photo, ranking_weights=weights,
                favorite_bonus=favorite_bonus, scoring_version_id=scoring_version_id, schema_kind="basic",
            )
            return {"analysis": result, "stage": "local", "_actual_cost": 0}

        if self.settings is not None and not force_ai and not bool(photo["eligible"]) and not bool(photo["manual_override"]):
            result = validate_analysis_result(self._prefilter_result(photo) or self._local_result(photo))
            result["should_keep"] = False
            raw = json.dumps(result, ensure_ascii=False)
            result = self._save_result(
                photo_id=photo_id, job_id=job_id, stage="prefilter", provider="local",
                model=FEATURE_VERSION, result=result, raw=raw, photo=photo, ranking_weights=weights,
                favorite_bonus=favorite_bonus, scoring_version_id=scoring_version_id, schema_kind="basic",
            )
            return {"analysis": result, "stage": "prefilter", "_actual_cost": 0}

        if not self._allow_ai_for_photo(photo_id, force_ai=force_ai) or self._photo_limits_reached():
            result = validate_analysis_result(self._local_result(photo))
            raw = json.dumps(result, ensure_ascii=False)
            result = self._save_result(
                photo_id=photo_id, job_id=job_id, stage="local_fallback", provider="local",
                model="local-quality-v3", result=result, raw=raw, photo=photo, ranking_weights=weights,
                favorite_bonus=favorite_bonus, scoring_version_id=scoring_version_id, schema_kind="basic",
            )
            return {"analysis": result, "stage": "local_fallback", "_actual_cost": 0}

        prefiltered = None if force_ai or self.settings is None else self._prefilter_result(photo)
        if prefiltered is not None:
            self.photos.persist_prefilter_exclusion(photo_id, self.prefilter_snapshot(photo))
            result = validate_analysis_result(prefiltered)
            raw = json.dumps(result, ensure_ascii=False)
            result = self._save_result(
                photo_id=photo_id, job_id=job_id, stage="prefilter", provider="local",
                model="local-prefilter", result=result, raw=raw, photo=photo, ranking_weights=weights,
                favorite_bonus=favorite_bonus, scoring_version_id=scoring_version_id, schema_kind="basic",
            )
            return {"analysis": result, "stage": "prefilter", "_actual_cost": 0}
        if provider is None:
            raise ValueError("VLM-008 尚未設定可用 Provider")

        sha = str(photo["sha256"] or "")
        if not sha:
            raise ValueError("IMG-003 照片尚未完成本地預處理")
        total_cost = 0.0

        def record_force(provider_name: str, model_name: str) -> None:
            if force_ai:
                self.photos.record_force_ai_event(
                    photo_id, job_id=job_id, provider=provider_name, model=model_name, actor=force_actor
                )

        if strategy in {"low_cost", "smart_two_stage"}:
            low_input = analysis_spec["low_vision_input"]
            low, raw, cost, cache_hit, actual_provider, actual_model, request_fingerprint, input_spec_json, _usage, _latency = self._model_call(
                provider=provider,
                image_factory=lambda: self.thumbnails.get_or_create(source, sha, 512),
                model=low_model,
                detail="low",
                stage="stage_one",
                job_id=job_id,
                photo_id=photo_id,
                content_sha256=sha,
                schema_kind="basic",
                caption_controls=caption_controls,
                prompt_version=prompt_version,
                vision_input=low_input,
                force_recompute=force_recompute,
            )
            total_cost += cost
            requires_second = strategy == "smart_two_stage" and (
                low["memory_score"] >= stage_two_threshold
                or "人物" in low["types"]
                or (favorite_override and bool(photo["favorite"]))
            )
            if not requires_second:
                low = self._save_result(
                    photo_id=photo_id, job_id=job_id, stage="stage_one", provider=actual_provider,
                    model=actual_model, result=low, raw=raw, photo=photo, ranking_weights=weights,
                    favorite_bonus=favorite_bonus, scoring_version_id=scoring_version_id, schema_kind="basic",
                    prompt_version=prompt_version,
                    analysis_fingerprint=analysis_fingerprint, analysis_spec_json=analysis_spec_json,
                    vision_request_fingerprint=request_fingerprint, vision_input_spec_json=input_spec_json,
                )
                record_force(actual_provider, actual_model)
                return {"analysis": low, "stage": "cache" if cache_hit else "stage_one", "_actual_cost": total_cost}

        high_input = analysis_spec["high_vision_input"]
        high, raw, cost, cache_hit, actual_provider, actual_model, request_fingerprint, input_spec_json, _usage, _latency = self._model_call(
            provider=provider,
            image_factory=lambda: self.thumbnails.get_or_create(source, sha, high_max_side),
            model=high_model,
            detail="high",
            stage="stage_two" if strategy == "smart_two_stage" else "single_high",
            job_id=job_id,
            photo_id=photo_id,
            content_sha256=sha,
            schema_kind="full",
            caption_controls=caption_controls,
            prompt_version=prompt_version,
            vision_input=high_input,
            force_recompute=force_recompute,
        )
        total_cost += cost
        final_stage = "stage_two" if strategy == "smart_two_stage" else "single_high"
        high = self._save_result(
            photo_id=photo_id, job_id=job_id, stage=final_stage, provider=actual_provider,
            model=actual_model, result=high, raw=raw, photo=photo, ranking_weights=weights,
            favorite_bonus=favorite_bonus, scoring_version_id=scoring_version_id, schema_kind="full",
            prompt_version=prompt_version,
            analysis_fingerprint=analysis_fingerprint, analysis_spec_json=analysis_spec_json,
            vision_request_fingerprint=request_fingerprint, vision_input_spec_json=input_spec_json,
        )
        record_force(actual_provider, actual_model)
        return {
            "analysis": high,
            "stage": "cache" if cache_hit else final_stage,
            "_actual_cost": total_cost,
        }
