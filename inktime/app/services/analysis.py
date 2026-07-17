from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time

from inktime.app.core.paths import safe_join
from inktime.app.domain.analysis import AnalysisValidationError, validate_analysis_result
from inktime.app.domain.photos import ThumbnailCache
from inktime.app.providers.base import ProviderResponse, VisionProvider
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.budgets import BudgetService


class PhotoAnalysisService:
    def __init__(self, photos: PhotoRepository, usage: UsageRepository, thumbnails: ThumbnailCache, budgets: BudgetService | None = None) -> None:
        self.photos = photos
        self.usage = usage
        self.thumbnails = thumbnails
        self.budgets = budgets

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

    def _record(self, provider: VisionProvider, model: str, job_id: str | None, photo_id: str, request_type: str, response: ProviderResponse, started_at: str, started_perf: float, retry_count: int = 0) -> float:
        cost = provider.estimate_cost(model, response.usage)
        self.usage.record(
            provider=provider.name, model=model, job_id=job_id, photo_id=photo_id,
            request_type=request_type, input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens, cached_tokens=response.usage.cached_tokens,
            estimated_cost=cost, actual_cost=cost, started_at=started_at,
            latency_ms=int((time.perf_counter() - started_perf) * 1000), status="completed",
            retry_count=retry_count,
        )
        return cost

    def _model_call(self, *, provider: VisionProvider, image: Path, model: str, detail: str, stage: str, job_id: str | None, photo_id: str) -> tuple[dict, str, float]:
        if self.budgets:
            self.budgets.assert_request_allowed(job_id, photo_id)
        started_at = datetime.now(timezone.utc).isoformat()
        started_perf = time.perf_counter()
        response = provider.analyze(image_path=image, model=model, detail=detail, stage=stage)
        total_cost = self._record(provider, model, job_id, photo_id, stage, response, started_at, started_perf)
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
            )
            total_cost += self._record(
                provider, model, job_id, photo_id, "json_repair", repaired,
                repair_started_at, repair_perf, retry_count=1,
            )
            # 第二次驗證失敗直接拋出；不得無限修復。
            result = validate_analysis_result(repaired.content)
            return result, repaired.content, total_cost

    def analyze_photo(self, *, photo_id: str, job_id: str | None, provider: VisionProvider | None, strategy: str, low_model: str = "low-cost-vision", high_model: str = "high-quality-vision", stage_two_threshold: float = 65, favorite_override: bool = True) -> dict:
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
            self.photos.save_analysis(photo_id, job_id, "local", "local", "local", result, raw)
            return {"analysis": result, "stage": "local", "_actual_cost": 0}
        if provider is None:
            raise ValueError("VLM-008 尚未設定可用 Provider")

        sha = str(photo["sha256"] or "")
        if not sha:
            raise ValueError("IMG-003 照片尚未完成本地預處理")
        total_cost = 0.0
        if strategy in {"low_cost", "smart_two_stage"}:
            low_image = self.thumbnails.get_or_create(source, sha, 512)
            low, raw, cost = self._model_call(
                provider=provider, image=low_image, model=low_model, detail="low", stage="stage_one",
                job_id=job_id, photo_id=photo_id,
            )
            total_cost += cost
            requires_second = strategy == "smart_two_stage" and (
                low["memory_score"] >= stage_two_threshold
                or "人物" in low["types"]
                or (favorite_override and bool(photo["favorite"]))
            )
            if not requires_second:
                self.photos.save_analysis(photo_id, job_id, "stage_one", provider.name, low_model, low, raw)
                return {"analysis": low, "stage": "stage_one", "_actual_cost": total_cost}

        high_image = self.thumbnails.get_or_create(source, sha, 1600)
        high, raw, cost = self._model_call(
            provider=provider, image=high_image, model=high_model, detail="high", stage="stage_two" if strategy == "smart_two_stage" else "single_high",
            job_id=job_id, photo_id=photo_id,
        )
        total_cost += cost
        self.photos.save_analysis(photo_id, job_id, "stage_two" if strategy == "smart_two_stage" else "single_high", provider.name, high_model, high, raw)
        return {"analysis": high, "stage": "stage_two" if strategy == "smart_two_stage" else "single_high", "_actual_cost": total_cost}
