from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time

from inktime.app.core.paths import safe_join
from inktime.app.domain.analysis import AnalysisValidationError, validate_analysis_result
from inktime.app.domain.analysis.scoring import (
    DEFAULT_FAVORITE_BONUS,
    DEFAULT_RANKING_WEIGHTS,
    calculate_ranking_score,
)
from inktime.app.domain.photos import ThumbnailCache
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


class PhotoAnalysisService:
    def __init__(
        self,
        photos: PhotoRepository,
        usage: UsageRepository,
        thumbnails: ThumbnailCache,
        budgets: BudgetService | None = None,
        settings: SettingsRepository | None = None,
    ) -> None:
        self.photos = photos
        self.usage = usage
        self.thumbnails = thumbnails
        self.budgets = budgets
        self.settings = settings or (budgets.settings if budgets else None)

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

    def _prefilter_result(self, photo) -> dict | None:
        if self.settings is None or not bool(self.settings.get("analysis.prefilter_enabled", True)):
            return None
        # 人工標記最愛是比本機啟發式更強的訊號，不自動攔截。
        if bool(photo["favorite"]):
            return None
        sensitivity = str(self.settings.get("analysis.prefilter_sensitivity", "conservative"))
        profile = PREFILTER_PROFILES.get(sensitivity, PREFILTER_PROFILES["conservative"])
        screenshot = (
            bool(self.settings.get("analysis.prefilter_screenshots", True))
            and float(photo["screenshot_likelihood"] or 0) >= profile["screenshot"]
        )

        defects: list[str] = []
        if photo["blur_score"] is not None and float(photo["blur_score"]) < profile["blur"]:
            defects.append("嚴重模糊或失焦")
        if photo["contrast"] is not None and float(photo["contrast"]) < profile["contrast"]:
            defects.append("對比過低")
        if photo["overexposed_ratio"] is not None and float(photo["overexposed_ratio"]) >= profile[
            "exposure"
        ]:
            defects.append("大面積過曝")
        if photo["underexposed_ratio"] is not None and float(photo["underexposed_ratio"]) >= profile[
            "exposure"
        ]:
            defects.append("大面積欠曝")
        if (
            photo["width"] is not None
            and photo["height"] is not None
            and min(int(photo["width"]), int(photo["height"])) < profile["short_edge"]
        ):
            defects.append("解析度過低")
        low_quality = bool(self.settings.get("analysis.prefilter_low_quality", True)) and len(defects) >= 2
        if not screenshot and not low_quality:
            return None

        quality = self._local_quality(photo)
        if screenshot:
            label = "截圖"
            reasons = ["本機截圖特徵達排除門檻"]
            memory_score = 5.0
            types = ["截圖"]
        else:
            label = "明顯低品質照片"
            reasons = defects
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
    ) -> tuple[dict, str, float]:
        if self.budgets:
            self.budgets.assert_request_allowed(job_id, photo_id)
        started_at = datetime.now(timezone.utc).isoformat()
        started_perf = time.perf_counter()
        max_tokens = int(self.budgets.settings.get("budget.max_tokens", 8000)) if self.budgets else None
        response = provider.analyze(
            image_path=image,
            model=model,
            detail=detail,
            stage=stage,
            max_tokens=max_tokens,
        )
        total_cost = self._record(
            provider, model, job_id, photo_id, stage, response, started_at, started_perf
        )
        try:
            result = validate_analysis_result(response.content)
            return result, response.content, total_cost
        except AnalysisValidationError as first_error:
            repair_started_at = datetime.now(timezone.utc).isoformat()
            repair_perf = time.perf_counter()
            repaired = provider.repair_json(
                invalid_content=response.content,
                validation_error=str(first_error),
                model=model,
                max_tokens=max_tokens,
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
            result = validate_analysis_result(repaired.content)
            return result, repaired.content, total_cost

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
    ) -> dict:
        photo = self.photos.get_with_path(photo_id)
        if photo is None:
            raise FileNotFoundError("SCAN-001 找不到照片資料")
        source = safe_join(Path(photo["root_path"]), str(photo["relative_path"]))
        if not source.is_file():
            raise FileNotFoundError("SCAN-001 找不到照片檔案")
        inherited = self.photos.inherit_existing_analysis(photo_id, job_id)
        if inherited is not None:
            return {"analysis": inherited, "stage": "inherited", "_actual_cost": 0}
        if strategy == "local":
            result = validate_analysis_result(self._local_result(photo))
            raw = json.dumps(result, ensure_ascii=False)
            result["ranking_score"] = calculate_ranking_score(
                result,
                ranking_weights or DEFAULT_RANKING_WEIGHTS,
                favorite=bool(photo["favorite"]),
                favorite_bonus=favorite_bonus,
            )
            self.photos.save_analysis(
                photo_id,
                job_id,
                "local",
                "local",
                "local",
                result,
                raw,
                ranking_score=result["ranking_score"],
                scoring_version_id=scoring_version_id,
            )
            return {"analysis": result, "stage": "local", "_actual_cost": 0}

        prefiltered = self._prefilter_result(photo)
        if prefiltered is not None:
            result = validate_analysis_result(prefiltered)
            raw = json.dumps(result, ensure_ascii=False)
            result["ranking_score"] = calculate_ranking_score(
                result,
                ranking_weights or DEFAULT_RANKING_WEIGHTS,
                favorite=False,
                favorite_bonus=favorite_bonus,
            )
            self.photos.save_analysis(
                photo_id,
                job_id,
                "prefilter",
                "local",
                "local-prefilter",
                result,
                raw,
                ranking_score=result["ranking_score"],
                scoring_version_id=scoring_version_id,
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
            low, raw, cost = self._model_call(
                provider=provider,
                image=low_image,
                model=low_model,
                detail="low",
                stage="stage_one",
                job_id=job_id,
                photo_id=photo_id,
            )
            total_cost += cost
            requires_second = strategy == "smart_two_stage" and (
                low["memory_score"] >= stage_two_threshold
                or "人物" in low["types"]
                or (favorite_override and bool(photo["favorite"]))
            )
            if not requires_second:
                low["ranking_score"] = calculate_ranking_score(
                    low,
                    ranking_weights or DEFAULT_RANKING_WEIGHTS,
                    favorite=bool(photo["favorite"]),
                    favorite_bonus=favorite_bonus,
                )
                self.photos.save_analysis(
                    photo_id,
                    job_id,
                    "stage_one",
                    provider.name,
                    low_model,
                    low,
                    raw,
                    ranking_score=low["ranking_score"],
                    scoring_version_id=scoring_version_id,
                )
                return {"analysis": low, "stage": "stage_one", "_actual_cost": total_cost}

        high_image = self.thumbnails.get_or_create(source, sha, 1600)
        high, raw, cost = self._model_call(
            provider=provider,
            image=high_image,
            model=high_model,
            detail="high",
            stage="stage_two" if strategy == "smart_two_stage" else "single_high",
            job_id=job_id,
            photo_id=photo_id,
        )
        total_cost += cost
        high["ranking_score"] = calculate_ranking_score(
            high,
            ranking_weights or DEFAULT_RANKING_WEIGHTS,
            favorite=bool(photo["favorite"]),
            favorite_bonus=favorite_bonus,
        )
        self.photos.save_analysis(
            photo_id,
            job_id,
            "stage_two" if strategy == "smart_two_stage" else "single_high",
            provider.name,
            high_model,
            high,
            raw,
            ranking_score=high["ranking_score"],
            scoring_version_id=scoring_version_id,
        )
        return {
            "analysis": high,
            "stage": "stage_two" if strategy == "smart_two_stage" else "single_high",
            "_actual_cost": total_cost,
        }
