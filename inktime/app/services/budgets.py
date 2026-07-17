from __future__ import annotations

from inktime.app.db import Database
from inktime.app.repositories.settings import SettingsRepository


class BudgetExceeded(RuntimeError):
    code = "BUDGET-002"


class BudgetService:
    def __init__(self, database: Database, settings: SettingsRepository) -> None:
        self.database = database
        self.settings = settings

    def snapshot(self, job_id: str | None = None, photo_id: str | None = None) -> dict:
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN date(started_at)=date('now') THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) daily,
                       COALESCE(SUM(CASE WHEN strftime('%Y-%m',started_at)=strftime('%Y-%m','now') THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) monthly,
                       COALESCE(SUM(CASE WHEN photo_id=? THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) photo
                FROM api_usage
                """,
                (photo_id,),
            ).fetchone()
            job = (
                connection.execute("SELECT spent,budget_limit FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job_id
                else None
            )
        return {
            "daily": float(row["daily"]),
            "monthly": float(row["monthly"]),
            "photo": float(row["photo"]),
            "job": float(job["spent"]) if job else 0,
            "job_limit": float(job["budget_limit"]) if job and job["budget_limit"] is not None else None,
        }

    def assert_request_allowed(self, job_id: str | None, photo_id: str) -> None:
        usage = self.snapshot(job_id, photo_id)
        checks = (
            (usage["daily"], float(self.settings.get("budget.daily_stop", 10)), "每日 API 預算已達停止值"),
            (
                usage["monthly"],
                float(self.settings.get("budget.monthly_stop", 100)),
                "每月 API 預算已達停止值",
            ),
            (usage["photo"], float(self.settings.get("budget.photo_max", 0.25)), "單張照片成本已達上限"),
        )
        for current, maximum, message in checks:
            if maximum > 0 and current >= maximum:
                raise BudgetExceeded(message)
        if usage["job_limit"] is not None and usage["job"] >= usage["job_limit"]:
            error = BudgetExceeded("工作預算已達上限")
            error.code = "BUDGET-001"
            raise error
