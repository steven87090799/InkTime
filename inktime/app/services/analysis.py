from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

from PIL import Image, ImageOps

from inktime.app.core.paths import safe_join
from inktime.app.domain.analysis import AnalysisValidationError, validate_analysis_result
from inktime.app.domain.analysis.scoring import (
    DEFAULT_FAVORITE_BONUS,
    DEFAULT_RANKING_WEIGHTS,
    calculate_ranking_score,
    calculate_travel_bonus,
    grade_to_score,
)
from inktime.app.domain.photos import ThumbnailCache
from inktime.app.domain.rendering import evaluate_e6_suitability
from inktime.app.providers.base import ProviderResponse, VisionProvider
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.budgets import BudgetService


PREFILTER_PROFILES = {
    "conservative": {
        "screenshot": 0.80,
        "blur": 12.0,
        "contrast": 7.0,
        "exposure": 0.94,
        "short_edge": 240,
    },
    "balanced": {
        "screenshot": 0.70,
        "blur": 25.0,
        "contrast": 12.0,
        "exposure": 0.90,
        "short_edge": 320,
    },
    "aggressive": {
        "screenshot": 0.60,
        "blur": 45.0,
        "contrast": 18.0,
        "exposure": 0.82,
        "short_edge": 480,
    },
}
PROMPT_VERSION = "photo-quality-v3"


class PhotoAnalysisService:
    def __init__(
        self,
        photos: PhotoRepository,
        usage: UsageRepository,
        thumbnails: ThumbnailCache,
        budgets: BudgetService | None = None,
        settings: SettingsRepository | None = None,
        observability=None,
    ) -> None:
        self.photos = photos
        self.usage = usage
        self.thumbnails = thumbnails
        self.budgets = budgets
        self.settings = settings or (budgets.settings if budgets else None)
        self.observability = observability

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
            "caption_target_chars": int(self.settings.get("analysis.caption_target_chars", 160)),
            "caption_max_chars": int(self.settings.get("analysis.caption_max_chars", 220)),
            "side_caption_min_chars": int(self.settings.get("analysis.side_caption_min_chars", 10)),
            "side_caption_target_chars": int(self.settings.get("analysis.side_caption_target_chars", 22)),
            "side_caption_max_chars": int(self.settings.get("analysis.side_caption_max_chars", 42)),
            "copy_default_style": str(self.settings.get("analysis.copy_default_style", "natural")),
            "copy_humor_level": int(self.settings.get("analysis.copy_humor_level", 1)),
            "copy_poetic_level": int(self.settings.get("analysis.copy_poetic_level", 1)),
            "copy_avoid_cliche": bool(self.settings.get("analysis.copy_avoid_cliche", True)),
            "copy_avoid_direct_description": bool(self.settings.get("analysis.copy_avoid_direct_description", True)),
            "copy_forbid_exclamation": bool(self.settings.get("analysis.copy_forbid_exclamation", True)),
            "copy_forbid_like_phrase": bool(self.settings.get("analysis.copy_forbid_like_phrase", True)),
            "copy_max_commas": int(self.settings.get("analysis.copy_max_commas", 2)),
            "copy_avoid_abstract_ending": bool(self.settings.get("analysis.copy_avoid_abstract_ending", True)),
            "copy_banned_words": lines("analysis.copy_banned_words"),
            "copy_banned_patterns": lines("analysis.copy_banned_patterns"),
            "copy_custom_rules": str(self.settings.get("analysis.copy_custom_rules", "")),
            "caption_variants_enabled": bool(self.settings.get("analysis.caption_variants_enabled", False)),
        }

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
            "schema_version": 1,
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
        enabled = self.settings is not None and bool(
            self.settings.get("analysis.prefilter_enabled", True)
        )
        sensitivity = (
            str(self.settings.get("analysis.prefilter_sensitivity", "conservative"))
            if self.settings is not None
            else "conservative"
        )
        profile = PREFILTER_PROFILES.get(sensitivity, PREFILTER_PROFILES["conservative"])
        screenshot_enabled = self.settings is not None and bool(
            self.settings.get("analysis.prefilter_screenshots", True)
        )
        low_quality_enabled = self.settings is not None and bool(
            self.settings.get("analysis.prefilter_low_quality", True)
        )
        e6_enabled = self.settings is not None and bool(
            self.settings.get("analysis.e6_prefilter_enabled", True)
        )
        e6_threshold = float(
            self.settings.get("analysis.e6_min_score", 25) if self.settings is not None else 25
        )
        favorite_bypass = bool(photo["favorite"])
        manual_override = bool(photo["manual_override"]) if "manual_override" in photo.keys() else False
        screenshot_score = float(photo["screenshot_likelihood"] or 0)
        blur = float(photo["blur_score"]) if photo["blur_score"] is not None else None
        contrast = float(photo["contrast"]) if photo["contrast"] is not None else None
        overexposed = (
            float(photo["overexposed_ratio"]) if photo["overexposed_ratio"] is not None else None
        )
        underexposed = (
            float(photo["underexposed_ratio"])
            if photo["underexposed_ratio"] is not None
            else None
        )
        short_edge = (
            min(int(photo["width"]), int(photo["height"]))
            if photo["width"] is not None and photo["height"] is not None
            else None
        )
        e6_score = float(photo["e6_score"]) if photo["e6_score"] is not None else None
        checks = [
            {
                "key": "screenshot",
                "label": "截圖機率",
                "value": f"{screenshot_score:.2f}",
                "threshold": f"≥ {profile['screenshot']:.2f}",
                "hit": screenshot_score >= profile["screenshot"],
                "enabled": screenshot_enabled,
            },
            {
                "key": "blur",
                "label": "嚴重模糊或失焦",
                "value": f"{blur:.2f}" if blur is not None else "無資料",
                "threshold": f"< {profile['blur']:.0f}",
                "hit": blur is not None and blur < profile["blur"],
                "enabled": low_quality_enabled,
            },
            {
                "key": "contrast",
                "label": "對比過低",
                "value": f"{contrast:.2f}" if contrast is not None else "無資料",
                "threshold": f"< {profile['contrast']:.0f}",
                "hit": contrast is not None and contrast < profile["contrast"],
                "enabled": low_quality_enabled,
            },
            {
                "key": "overexposed",
                "label": "大面積過曝",
                "value": f"{overexposed * 100:.2f}%" if overexposed is not None else "無資料",
                "threshold": f"≥ {profile['exposure'] * 100:.0f}%",
                "hit": overexposed is not None and overexposed >= profile["exposure"],
                "enabled": low_quality_enabled,
            },
            {
                "key": "underexposed",
                "label": "大面積欠曝",
                "value": f"{underexposed * 100:.2f}%" if underexposed is not None else "無資料",
                "threshold": f"≥ {profile['exposure'] * 100:.0f}%",
                "hit": underexposed is not None and underexposed >= profile["exposure"],
                "enabled": low_quality_enabled,
            },
            {
                "key": "resolution",
                "label": "解析度過低（短邊）",
                "value": f"{short_edge} px" if short_edge is not None else "無資料",
                "threshold": f"< {profile['short_edge']} px",
                "hit": short_edge is not None and short_edge < profile["short_edge"],
                "enabled": low_quality_enabled,
            },
            {
                "key": "e6_suitability",
                "label": "E6 六色適合度過低",
                "value": f"{e6_score:.2f}" if e6_score is not None else "尚未計算",
                "threshold": f"< {e6_threshold:.0f}",
                "hit": e6_score is not None and e6_score < e6_threshold,
                "enabled": e6_enabled,
            },
        ]
        defect_checks = checks[1:-1]
        matched_defects = [check["label"] for check in defect_checks if check["hit"]]
        screenshot = enabled and screenshot_enabled and checks[0]["hit"] and not favorite_bypass and not manual_override
        low_quality = (
            enabled
            and low_quality_enabled
            and len(matched_defects) >= 2
            and not favorite_bypass
            and not manual_override
        )
        e6_unsuitable = enabled and e6_enabled and checks[-1]["hit"] and not favorite_bypass and not manual_override
        if not enabled:
            decision = "disabled"
            summary = "本機預篩選已停用"
        elif favorite_bypass:
            decision = "favorite_bypass"
            summary = "最愛照片略過本機預篩選"
        elif manual_override:
            decision = "manual_override"
            summary = "人工恢復覆寫生效；內容未改變前不會再次自動排除"
        elif screenshot:
            decision = "excluded_screenshot"
            summary = "已排除：截圖機率達門檻，不會呼叫模型"
        elif low_quality:
            decision = "excluded_low_quality"
            summary = f"已排除：命中 {len(matched_defects)} 項品質缺陷，不會呼叫模型"
        elif e6_unsuitable:
            decision = "excluded_e6"
            summary = "已排除：E6 六色量化後適合度過低，不會呼叫模型"
        else:
            decision = "passed"
            summary = f"通過本機預篩選：命中 {len(matched_defects)} 項品質缺陷"
        return {
            "enabled": enabled,
            "sensitivity": sensitivity,
            "screenshot_enabled": screenshot_enabled,
            "low_quality_enabled": low_quality_enabled,
            "e6_enabled": e6_enabled,
            "e6_threshold": e6_threshold,
            "favorite_bypass": favorite_bypass,
            "manual_override": manual_override,
            "checks": checks,
            "matched_defects": matched_defects,
            "defect_count": len(matched_defects),
            "required_defects": 2,
            "decision": decision,
            "summary": summary,
            "excluded": screenshot or low_quality or e6_unsuitable,
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
            reasons = evaluation["matched_defects"]
            memory_score = 15.0
            types = ["其他"]
        return {
            "schema_version": 1,
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
    ) -> tuple[dict, str, float, bool]:
        cached = self.photos.get_ai_cache(
            content_sha256=content_sha256,
            provider=provider.name,
            model_name=model,
            prompt_version=prompt_version,
            schema_version=1,
            schema_kind=schema_kind,
        )
        if cached is not None:
            try:
                self._activity("DEBUG", "caption_cache_hit", "Caption AI Cache 命中", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=prompt_version)
                return validate_analysis_result(cached["result"]), str(cached["raw_json"]), 0.0, True
            except AnalysisValidationError:
                # 損壞或舊版快取不可阻斷新分析，直接重新取得並覆寫。
                pass
        cache_key = hashlib.sha256(
            json.dumps(
                [content_sha256, provider.name, model, prompt_version, 1, schema_kind],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._activity("DEBUG", "caption_cache_miss", "Caption AI Cache 未命中", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=prompt_version)
        owner_id = str(uuid4())
        deadline = time.monotonic() + 120
        while not self.photos.acquire_ai_cache_reservation(cache_key, owner_id):
            if time.monotonic() >= deadline:
                raise TimeoutError("AI-CACHE-001 等待相同分析結果逾時")
            time.sleep(0.05)
            cached = self.photos.get_ai_cache(
                content_sha256=content_sha256,
                provider=provider.name,
                model_name=model,
                prompt_version=prompt_version,
                schema_version=1,
                schema_kind=schema_kind,
            )
            if cached is not None:
                self._activity("DEBUG", "caption_cache_hit", "等待中的 Caption AI Cache 已完成", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=prompt_version)
                return validate_analysis_result(cached["result"]), str(cached["raw_json"]), 0.0, True
        try:
            result, raw, cost = self._perform_uncached_model_call(
                provider=provider,
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
            )
        except Exception as exc:
            self.photos.finish_ai_cache_reservation(cache_key, owner_id, error=str(exc))
            raise
        self.photos.finish_ai_cache_reservation(cache_key, owner_id)
        return result, raw, cost, False

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
    ) -> tuple[dict, str, float]:
        if self.budgets:
            self.budgets.assert_request_allowed(job_id, photo_id)
        started_at = datetime.now(timezone.utc).isoformat()
        started_perf = time.perf_counter()
        max_tokens = int(self.budgets.settings.get("budget.max_tokens", 8000)) if self.budgets else None
        self._activity("DEBUG", "provider_request_started", "Caption Provider 請求開始", job_id=job_id, photo_id=photo_id, stage=stage, trace_id=prompt_version, provider=provider.name, model=model)
        try:
            response = provider.analyze(image_path=image, model=model, detail=detail, stage=stage, max_tokens=max_tokens, caption_controls=caption_controls)
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
            repaired = provider.repair_json(
                invalid_content=response.content,
                validation_error=str(first_error),
                model=model,
                max_tokens=max_tokens,
                stage=stage,
                caption_controls=caption_controls,
            )
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
            schema_kind=schema_kind,
            result=result,
            raw_json=raw,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cached_tokens=total_cached_tokens,
            estimated_cost=total_cost,
            latency_ms=int((time.perf_counter() - started_perf) * 1000),
        )
        return result, raw, total_cost

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
    ) -> dict:
        photo = self.photos.get_with_path(photo_id)
        if photo is None:
            raise FileNotFoundError("SCAN-001 找不到照片資料")
        source = safe_join(Path(photo["root_path"]), str(photo["relative_path"]))
        if not source.is_file():
            raise FileNotFoundError("SCAN-001 找不到照片檔案")
        photo = self._ensure_e6_suitability(photo_id, photo, source)
        caption_controls = self._caption_controls()
        prompt_version = self._prompt_version(caption_controls)
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

        if not bool(photo["eligible"]) and not bool(photo["manual_override"]):
            result = validate_analysis_result(self._prefilter_result(photo) or self._local_result(photo))
            result["should_keep"] = False
            raw = json.dumps(result, ensure_ascii=False)
            result = self._save_result(
                photo_id=photo_id, job_id=job_id, stage="prefilter", provider="local",
                model="local-quality-v3", result=result, raw=raw, photo=photo, ranking_weights=weights,
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

        prefiltered = self._prefilter_result(photo)
        if prefiltered is not None:
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
        if strategy in {"low_cost", "smart_two_stage"}:
            low_image = self.thumbnails.get_or_create(source, sha, 512)
            low, raw, cost, cache_hit = self._model_call(
                provider=provider,
                image=low_image,
                model=low_model,
                detail="low",
                stage="stage_one",
                job_id=job_id,
                photo_id=photo_id,
                content_sha256=sha,
                schema_kind="basic",
                caption_controls=caption_controls,
                prompt_version=prompt_version,
            )
            total_cost += cost
            requires_second = strategy == "smart_two_stage" and (
                low["memory_score"] >= stage_two_threshold
                or "人物" in low["types"]
                or (favorite_override and bool(photo["favorite"]))
            )
            if not requires_second:
                low = self._save_result(
                    photo_id=photo_id, job_id=job_id, stage="stage_one", provider=provider.name,
                    model=low_model, result=low, raw=raw, photo=photo, ranking_weights=weights,
                    favorite_bonus=favorite_bonus, scoring_version_id=scoring_version_id, schema_kind="basic",
                    prompt_version=prompt_version,
                )
                return {"analysis": low, "stage": "cache" if cache_hit else "stage_one", "_actual_cost": total_cost}

        high_image = self.thumbnails.get_or_create(source, sha, 1600)
        high, raw, cost, cache_hit = self._model_call(
            provider=provider,
            image=high_image,
            model=high_model,
            detail="high",
            stage="stage_two" if strategy == "smart_two_stage" else "single_high",
            job_id=job_id,
            photo_id=photo_id,
            content_sha256=sha,
            schema_kind="full",
            caption_controls=caption_controls,
            prompt_version=prompt_version,
        )
        total_cost += cost
        final_stage = "stage_two" if strategy == "smart_two_stage" else "single_high"
        high = self._save_result(
            photo_id=photo_id, job_id=job_id, stage=final_stage, provider=provider.name,
            model=high_model, result=high, raw=raw, photo=photo, ranking_weights=weights,
            favorite_bonus=favorite_bonus, scoring_version_id=scoring_version_id, schema_kind="full",
            prompt_version=prompt_version,
        )
        return {
            "analysis": high,
            "stage": "cache" if cache_hit else final_stage,
            "_actual_cost": total_cost,
        }
