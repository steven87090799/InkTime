from __future__ import annotations

from inktime.app.repositories.jobs import JobRepository


class InvalidJobTransition(ValueError):
    code = "JOB-001"


class JobService:
    STRATEGIES = {"local", "low_cost", "high_quality", "smart_two_stage", "custom"}

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
    ) -> str:
        if strategy not in self.STRATEGIES:
            raise ValueError("不支援的分析策略")
        if budget_limit is not None and budget_limit < 0:
            raise ValueError("預算不可小於零")
        selected = photo_ids if photo_ids is not None else self.repository.iter_photo_ids(limit=limit)
        return self.repository.create(
            name=name.strip() or "未命名分析工作",
            strategy=strategy,
            settings=settings,
            photo_ids=selected,
            created_by=created_by,
            budget_limit=budget_limit,
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
        first = 0 if strategy == "local" else photo_count
        second = (
            photo_count
            if strategy == "high_quality"
            else int(photo_count * second_stage_ratio)
            if strategy == "smart_two_stage"
            else 0
        )
        average = first * low_cost_per_photo + second * high_cost_per_photo
        return {
            "photos": photo_count,
            "stage_one_photos": first,
            "stage_two_photos": second,
            "estimated_input_tokens": first * 1000 + second * 2500,
            "estimated_output_tokens": first * 180 + second * 500,
            "minimum_cost": round(average * 0.7, 4),
            "average_cost": round(average, 4),
            "maximum_cost": round(average * 1.5, 4),
        }
