from __future__ import annotations

import json

from inktime.app.domain.analysis.plan import DEFAULT_ANALYSIS_TOKEN_CAP, fingerprint, normalize_analysis_strategy, stored_analysis_fingerprint
from inktime.app.domain.jobs.failure_policy import NoEligibleCandidatesError
from inktime.app.domain.jobs.settings import validate_worker_settings
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
        request_fingerprint: str | None = None,
    ) -> str:
        if strategy not in self.STRATEGIES:
            raise ValueError("不支援的分析策略")
        strategy = normalize_analysis_strategy(strategy)
        settings = validate_worker_settings(settings)
        if budget_limit is not None and budget_limit < 0:
            raise ValueError("預算不可小於零")
        if dedupe_key:
            existing = self.repository.get_by_dedupe_key(
                dedupe_key, request_fingerprint=request_fingerprint
            )
            if existing is not None:
                return str(existing["id"])
        alternate = (
            fingerprint(analysis_spec)
            if analysis_spec and analysis_fingerprint == stored_analysis_fingerprint(analysis_spec)
            else None
        )
        if photo_ids is None:
            preview = self.repository.selection_preview(
                analysis_fingerprint=analysis_fingerprint, selection_mode=selection_mode, limit=limit,
                alternate_analysis_fingerprint=alternate,
            )
            if not preview["limited_to"]:
                raise NoEligibleCandidatesError("目前沒有符合條件的待分析照片")
            selected = self.repository.iter_pending_photo_ids(
                analysis_fingerprint=analysis_fingerprint, selection_mode=selection_mode, limit=limit,
                alternate_analysis_fingerprint=alternate,
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
            request_fingerprint=request_fingerprint,
        )

    def start(self, job_id: str) -> None:
        if not self.repository.transition(job_id, {"pending"}, "running", "started"):
            raise InvalidJobTransition("目前狀態無法啟動")

    def pause(self, job_id: str) -> None:
        if not self.repository.request_pause(job_id):
            raise InvalidJobTransition("目前狀態無法暫停")
        job = self.repository.get(job_id)
        if job and json.loads(job["settings_json"]).get("complementary_lane") == "openai_backfill":
            with self.repository.database.session() as connection:
                connection.execute("UPDATE openai_backfill_control SET pause_kind='manual',pause_reason='使用者已暫停工作',resume_job_id=NULL WHERE id=1")

    def resume(self, job_id: str) -> None:
        if not self.repository.transition(job_id, {"paused", "budget_exceeded"}, "running", "resumed"):
            raise InvalidJobTransition("目前狀態無法繼續")

    def cancel(self, job_id: str) -> None:
        if not self.repository.cancel(job_id):
            raise InvalidJobTransition("目前狀態無法取消")

    def retry_failed(self, job_id: str) -> int:
        return self.repository.retry_failed(job_id)

    def retry_failed_with_current_plan(
        self,
        job_id: str,
        *,
        analysis_spec: dict,
        created_by: str,
    ) -> dict:
        """Create a new current-route job containing only the old failed photos."""

        original = self.repository.get(job_id)
        if original is None or str(original["kind"]) != "analysis":
            raise InvalidJobTransition("只有照片分析工作可改用目前模型補救")
        if str(original["status"]) not in {"failed", "completed_with_errors"}:
            raise InvalidJobTransition("目前工作沒有可建立補救工作的失敗狀態")
        strategy = normalize_analysis_strategy(original["strategy"])
        if strategy == "local":
            raise InvalidJobTransition("本機工作沒有可改用模型補救的項目")
        photo_ids = self.repository.failed_photo_ids(job_id)
        if not photo_ids:
            raise InvalidJobTransition("沒有可改用目前模型重試的失敗照片")

        analysis_fingerprint = stored_analysis_fingerprint(analysis_spec)
        dedupe_key = f"retry-current-route:{job_id}:{analysis_fingerprint}"
        existing = self.repository.get_by_dedupe_key(dedupe_key)
        if existing is not None:
            return {
                "id": str(existing["id"]),
                "affected": len(photo_ids),
                "created": False,
                "status": str(existing["status"]),
            }

        rescue_id = self.create_analysis_job(
            name=f"{str(original['name'])}－失敗照片補救",
            strategy=strategy,
            settings={
                "force_ai": True,
                "source": "retry-failed-current-route",
                "parent_job_id": job_id,
            },
            created_by=created_by,
            budget_limit=original["budget_limit"],
            photo_ids=photo_ids,
            priority=int(original["priority"] or 3),
            dedupe_key=dedupe_key,
            selection_mode="force_all",
            analysis_fingerprint=analysis_fingerprint,
            force_recompute=True,
            analysis_spec=analysis_spec,
        )
        return {
            "id": rescue_id,
            "affected": len(photo_ids),
            "created": True,
            "status": "pending",
        }

    def estimate(
        self,
        photo_count: int,
        strategy: str,
        *,
        pricing: dict | None = None,
        input_tokens: int = 8000,
        output_tokens: int = DEFAULT_ANALYSIS_TOKEN_CAP,
    ) -> dict:
        normalized = normalize_analysis_strategy(strategy)
        image_calls = 0 if normalized == "local" else photo_count
        from inktime.app.providers.base import Usage
        from inktime.app.providers.openai_compatible import calculate_usage_cost
        unit = calculate_usage_cost(pricing, Usage(input_tokens, output_tokens))
        average = 0.0 if image_calls == 0 else None if unit is None else image_calls * unit
        return {
            "price_known": average is not None,
            "estimate_basis": "輸入 Token 為假設，未量測圖片 Token；0.5–2 倍為費用情境，非保證上下限；實際帳單依供應商回報",
            "estimate_is_bound": False,
            "input_tokens_measured": False,
            "cache_savings_included": False,
            "photos": photo_count,
            "image_calls": image_calls,
            # These names remain in the response for old dashboards; the
            # second-stage count is permanently zero under the new contract.
            "stage_one_photos": image_calls,
            "stage_two_photos": 0,
            "estimated_input_tokens": image_calls * input_tokens,
            "estimated_output_tokens": image_calls * output_tokens,
            "minimum_cost": 0.0 if image_calls == 0 else None if average is None else round(average * 0.5, 4),
            "average_cost": None if average is None else round(average, 4),
            "maximum_cost": None if average is None else round(average * 2, 4),
        }
