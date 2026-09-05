from __future__ import annotations

from inktime.app.db import Database
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.services.usage_periods import usage_periods


class BudgetExceeded(RuntimeError):
    code = "BUDGET-002"


class BudgetService:
    def __init__(self, database: Database, settings: SettingsRepository) -> None:
        self.database = database
        self.settings = settings

    @staticmethod
    def _billable_evidence_sql(alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        return f"""(
            COALESCE({prefix}input_tokens,0) > 0 OR COALESCE({prefix}output_tokens,0) > 0
            OR COALESCE({prefix}cached_tokens,0) > 0 OR COALESCE({prefix}reasoning_tokens,0) > 0
            OR COALESCE({prefix}cache_write_tokens,0) > 0 OR COALESCE({prefix}request_body_bytes,0) > 0
            OR COALESCE({prefix}image_bytes,0) > 0 OR COALESCE({prefix}actual_cost,0) > 0
            OR COALESCE({prefix}estimated_cost,0) > 0
        )"""

    def snapshot(self, job_id: str | None = None, photo_id: str | None = None) -> dict:
        evidence = self._billable_evidence_sql()
        periods = usage_periods(str(self.settings.get("general.timezone", "Asia/Taipei")))
        with self.database.session() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN cost_source<>'unknown'
                        AND started_at >= :day_start AND started_at < :day_end THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) daily_known,
                    COALESCE(SUM(CASE WHEN cost_source<>'unknown'
                        AND started_at >= :month_start AND started_at < :month_end
                        THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) monthly_known,
                    COALESCE(SUM(CASE WHEN cost_source<>'unknown' AND photo_id=:photo_id
                        THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) photo_known,
                    COALESCE(SUM(CASE WHEN cost_source='unknown' AND {evidence} THEN 1 ELSE 0 END),0) unknown_count,
                    COALESCE(SUM(CASE WHEN cost_source='unknown' AND {evidence}
                        AND started_at >= :day_start AND started_at < :day_end THEN 1 ELSE 0 END),0) daily_unknown_count,
                    COALESCE(SUM(CASE WHEN cost_source='unknown' AND {evidence}
                        AND started_at >= :month_start AND started_at < :month_end THEN 1 ELSE 0 END),0) monthly_unknown_count,
                    COALESCE(SUM(CASE WHEN cost_source='unknown' AND {evidence} AND photo_id=:photo_id THEN 1 ELSE 0 END),0) photo_unknown_count,
                    COALESCE(SUM(CASE WHEN cost_source='unknown' AND {evidence} AND job_id=:job_id THEN 1 ELSE 0 END),0) job_unknown_count
                FROM api_usage
                """,
                periods | {"photo_id": photo_id, "job_id": job_id},
            ).fetchone()
            job = (
                connection.execute("SELECT spent,budget_limit FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job_id
                else None
            )

        reserve = max(0.01, min(100.0, float(self.settings.get("budget.unknown_request_reserve", 0.25))))
        daily_known = float(row["daily_known"] or 0)
        monthly_known = float(row["monthly_known"] or 0)
        photo_known = float(row["photo_known"] or 0)
        daily_unknown = int(row["daily_unknown_count"] or 0)
        monthly_unknown = int(row["monthly_unknown_count"] or 0)
        photo_unknown = int(row["photo_unknown_count"] or 0)
        job_unknown = int(row["job_unknown_count"] or 0)
        job_known = float(job["spent"] or 0) if job else 0.0
        job_limit = float(job["budget_limit"]) if job and job["budget_limit"] is not None else None
        snapshot = {
            "daily_known": daily_known,
            "monthly_known": monthly_known,
            "photo_known": photo_known,
            "job_known": job_known,
            "daily_unknown_count": daily_unknown,
            "monthly_unknown_count": monthly_unknown,
            "photo_unknown_count": photo_unknown,
            "job_unknown_count": job_unknown,
            "unknown_count": int(row["unknown_count"] or 0),
            "unknown_request_reserve": reserve,
            "daily_effective": daily_known + daily_unknown * reserve,
            "monthly_effective": monthly_known + monthly_unknown * reserve,
            # Unknown usage stays visible and contributes to installation-wide
            # daily/monthly risk.  It must not permanently poison one photo or
            # a later Job, especially after routing moves to a different
            # Provider that reports an authoritative cost.
            "photo_effective": photo_known,
            "job_effective": job_known,
            "job_limit": job_limit,
        }
        # Keep the old keys as effective values so existing callers enforce the
        # new reserve model without silently dropping historical fields.
        snapshot.update(
            daily=snapshot["daily_effective"],
            monthly=snapshot["monthly_effective"],
            photo=snapshot["photo_effective"],
            job=snapshot["job_effective"],
        )
        return snapshot

    def assert_request_allowed(self, job_id: str | None, photo_id: str | None) -> None:
        usage = self.snapshot(job_id, photo_id)
        checks = (
            (
                usage["daily_effective"],
                float(self.settings.get("budget.daily_stop", 10)),
                "每日 API 預算已達停止值",
            ),
            (
                usage["monthly_effective"],
                float(self.settings.get("budget.monthly_stop", 100)),
                "每月 API 預算已達停止值",
            ),
            (
                usage["photo_known"],
                float(self.settings.get("budget.photo_max", 0.25)),
                "單張照片已確認成本達到上限",
            ),
        )
        for current, maximum, message in checks:
            if maximum > 0 and current >= maximum:
                raise BudgetExceeded(message)
        if usage["job_limit"] is not None and usage["job_known"] >= usage["job_limit"]:
            error = BudgetExceeded("工作預算已達上限")
            error.code = "BUDGET-001"
            raise error
