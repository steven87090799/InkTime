from __future__ import annotations

from inktime.app.domain.analysis.plan import normalize_analysis_strategy
from inktime.app.repositories.jobs import JobRepository


class InvalidJobTransition(ValueError):
    code = "JOB-001"


class JobService:
    STRATEGIES = {"local", "single", "low_cost", "high_quality", "single_high", "smart", "smart_two_stage", "custom"}

    def __init__(self, repository: JobRepository) -> None:
        self.repository = repository

    def create_analysis_job(
        self,
        *,
        name: str,
        strategy: str,
        settings: dict,
        created_by: str,
        budget_limit: float | None,
        limit: int | None = None,
        photo_ids=None,
        priority: int = 3,
        dedupe_key: str | None = None,
        selection_mode: str = "pending",
        analysis_fingerprint: str | None = None,
        force_recompute: bool = False,
        analysis_spec: dict | None = None,
    ) -> str:
        if strategy not in self.STRATEGIES:
            raise ValueError("不支援的分析策略")
        strategy = normalize_analysis_strategy(strategy)
        if budget_limit is not None and budget_limit < 0:
            raise ValueError("預算不可小於零")
        if photo_ids is None:
            preview = self.repository.selection_preview(
                analysis_fingerprint=analysis_fingerprint, selection_mode=selection_mode, limit=limit
            )
            if not preview["limited_to"]:
                raise ValueError("目前沒有符合條件的待分析照片")
            selected = self.repository.iter_pending_photo_ids(
                analysis_fingerprint=analysis_fingerprint, selection_mode=selection_mode, limit=limit
            )
        else:
            selected = photo_ids
        return self.repository.create(
            name=name.strip() or "未命名分析工作",
            strategy=strategy,
            settings=settings,
            photo_ids=selected,
            created_by=created_by,
            budget_limit=budget_limit,
            priority=priority,
            dedupe_key=dedupe_key,
            selection_mode=selection_mode,
            analysis_fingerprint=analysis_fingerprint,
            force_recompute=force_recompute,
            analysis_spec=analysis_spec,
        )

    def start(self, job_id: str) -> None:
        if not self.repository.transition(job_id, {"pending"}, "running", "started"):
            raise InvalidJobTransition("目前狀態無法啟動")

    def pause(self, job_id: str) -> None:
        if not self.repository.request_pause(job_id):
            raise InvalidJobTransition("目前狀態無法暫停")

    def resume(self, job_id: str) -> None:
        if not self.repository.transition(job_id, {"paused", "budget_exceeded"}, "running", "resumed"):
            raise InvalidJobTransition("目前狀態無法繼續")

    def cancel(self, job_id: str) -> None:
        if not self.repository.cancel(job_id):
            raise InvalidJobTransition("目前狀態無法取消")

    def retry_failed(self, job_id: str) -> int:
        return self.repository.retry_failed(job_id)

    def estimate(
        self,
        photo_count: int,
        strategy: str,
        *,
        low_cost_per_photo: float = 0.001,
        high_cost_per_photo: float = 0.01,
        second_stage_ratio: float = 0.35,
    ) -> dict:
        normalized = normalize_analysis_strategy(strategy)
        image_calls = 0 if normalized == "local" else photo_count
        average = image_calls * high_cost_per_photo
        return {
            "photos": photo_count,
            "image_calls": image_calls,
            # These names remain in the response for old dashboards; the
            # second-stage count is permanently zero under the new contract.
            "stage_one_photos": image_calls,
            "stage_two_photos": 0,
            "estimated_input_tokens": image_calls * 2500,
            "estimated_output_tokens": image_calls * 500,
            "minimum_cost": round(average * 0.7, 4),
            "average_cost": round(average, 4),
            "maximum_cost": round(average * 1.5, 4),
        }
