from __future__ import annotations

from datetime import datetime, timezone
import math
import time
from uuid import uuid4

from inktime.app.db import Database
from inktime.app.providers.base import Usage
from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.repositories.usage import UsageRepository
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
            OR {prefix}status IN ('completed','success')
        )"""

    def snapshot(self, job_id: str | None = None, photo_id: str | None = None) -> dict:
        evidence = self._billable_evidence_sql()
        periods = usage_periods(str(self.settings.get("general.timezone", "Asia/Taipei")))
        with self.database.session() as connection:
            def totals(where: str, parameters: tuple = ()) -> tuple[float, int]:
                result = connection.execute(
                    f"SELECT COALESCE(SUM(CASE WHEN cost_source<>'unknown' "
                    f"THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0), "
                    f"COALESCE(SUM(CASE WHEN cost_source='unknown' AND {evidence} "
                    f"THEN 1 ELSE 0 END),0) FROM api_usage WHERE {where}",
                    parameters,
                ).fetchone()
                return float(result[0]), int(result[1])

            daily_known, daily_unknown = totals(
                "started_at>=? AND started_at<?", (periods["day_start"], periods["day_end"])
            )
            monthly_known, monthly_unknown = totals(
                "started_at>=? AND started_at<?", (periods["month_start"], periods["month_end"])
            )
            photo_known, photo_unknown = totals("photo_id=?", (photo_id,))
            job_known, job_unknown = totals("job_id=?", (job_id,))
            photo_known += float(connection.execute(
                "SELECT COALESCE(SUM(cost),0) FROM usage_cost_archive WHERE photo_id=?", (photo_id,),
            ).fetchone()[0])
            job_known += float(connection.execute(
                "SELECT COALESCE(SUM(cost),0) FROM usage_cost_archive WHERE job_id=?", (job_id,),
            ).fetchone()[0])
            unknown_count = connection.execute(
                f"SELECT COUNT(*) FROM api_usage WHERE cost_source='unknown' AND {evidence}"
            ).fetchone()[0]
            job = (
                connection.execute("SELECT budget_limit FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job_id else None
            )
            reservations = connection.execute(
                "SELECT COALESCE(SUM(amount),0) total, "
                "COALESCE(SUM(CASE WHEN job_id=? THEN amount ELSE 0 END),0) job, "
                "COALESCE(SUM(CASE WHEN photo_id=? THEN amount ELSE 0 END),0) photo "
                "FROM budget_reservations WHERE state='active'", (job_id, photo_id),
            ).fetchone()

        reserve = max(0.01, min(100.0, float(self.settings.get("budget.unknown_request_reserve", 0.25))))
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
            "unknown_count": int(unknown_count),
            "unknown_request_reserve": reserve,
            "daily_effective": daily_known + daily_unknown * reserve + float(reservations["total"]),
            "monthly_effective": monthly_known + monthly_unknown * reserve + float(reservations["total"]),
            "reserved": float(reservations["total"]),
            # Unknown usage stays visible and contributes to installation-wide
            # daily/monthly risk.  It must not permanently poison one photo or
            # a later Job, especially after routing moves to a different
            # Provider that reports an authoritative cost.
            "photo_effective": photo_known + float(reservations["photo"]),
            "job_effective": job_known + float(reservations["job"]),
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

    def reserve(self, reservation_id: str, amount: float, *, job_id=None, photo_id=None) -> None:
        """Serialize projected-budget checks with every new paid reservation."""
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Invalid budget reservation")
        with self.database.transaction(operation="budget_reserve") as connection:
            if connection.execute("SELECT 1 FROM budget_reservations WHERE id=? AND state='active'", (reservation_id,)).fetchone():
                return
            snapshot = self.snapshot(job_id, photo_id)
            if snapshot["job_limit"] is not None and snapshot["job_effective"] + amount > snapshot["job_limit"]:
                raise BudgetExceeded("付費請求預留金額超過工作預算")
            for current, maximum in (
                (snapshot["daily_effective"], float(self.settings.get("budget.daily_stop", 10))),
                (snapshot["monthly_effective"], float(self.settings.get("budget.monthly_stop", 100))),
                (snapshot["photo_effective"], float(self.settings.get("budget.photo_max", 0.25)) if photo_id else 0),
                (snapshot["job_effective"], snapshot["job_limit"] or 0),
            ):
                if maximum > 0 and current + amount > maximum:
                    raise BudgetExceeded("付費請求預留金額超過可用預算")
            connection.execute(
                "INSERT INTO budget_reservations(id,amount,job_id,photo_id,state,created_at) "
                "VALUES (?,?,?,?,'active',?) ON CONFLICT(id) DO UPDATE SET "
                "amount=excluded.amount,job_id=excluded.job_id,photo_id=excluded.photo_id,state='active',created_at=excluded.created_at",
                (reservation_id, amount, job_id, photo_id, datetime.now(timezone.utc).isoformat()),
            )

    def release(self, reservation_id: str) -> None:
        with self.database.session() as connection:
            connection.execute("UPDATE budget_reservations SET state='released' WHERE id=?", (reservation_id,))

    def estimate_reserve(self, provider, model: str, *, output_tokens: int, batch: bool = False) -> float:
        floor = max(0.01, float(self.settings.get("budget.unknown_request_reserve", 0.25)))
        estimate = (provider.estimate_batch_cost if batch else provider.estimate_cost)(
            model, Usage(input_tokens=max(8000, int(self.settings.get("budget.max_tokens", 8000))),
                         output_tokens=output_tokens),
        )
        return max(0.0, float(estimate)) if estimate is not None else floor

    def call(self, provider, method: str, **kwargs):
        """Shared paid-call gate for diagnostics and live benchmark subrequests."""
        reservation_id = str(uuid4())
        model = str(kwargs["model"])
        self.reserve(reservation_id, self.estimate_reserve(
            provider, model, output_tokens=int(kwargs.get("max_tokens") or 2048)))
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        try:
            response = getattr(provider, method)(**kwargs)
        except ProviderHTTPError as error:
            if not error.ambiguous:
                self.release(reservation_id)
            raise
        usage = response.usage
        estimated = provider.estimate_cost(str(response.served_model or model), usage) if usage.tokens_reported else None
        actual = usage.provider_reported_cost
        UsageRepository(self.database).record(
            provider=provider.name, provider_id=getattr(provider, "provider_id", None),
            model=str(response.served_model or model), job_id=None, photo_id=None,
            request_type="diagnostic_" + method, tokens_reported=usage.tokens_reported, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens, estimated_cost=estimated, actual_cost=actual,
            started_at=started_at, latency_ms=int((time.monotonic()-started)*1000), status="completed",
            request_id=response.request_id, reasoning_tokens=usage.reasoning_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_source="provider_reported" if actual is not None else "estimated" if estimated is not None else "unknown",
        )
        if actual is not None or estimated is not None:
            self.release(reservation_id)
        return response

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
                usage["photo_effective"],
                float(self.settings.get("budget.photo_max", 0.25)),
                "單張照片已確認成本達到上限",
            ),
        )
        for current, maximum, message in checks:
            if maximum > 0 and current >= maximum:
                raise BudgetExceeded(message)
        if usage["job_limit"] is not None and usage["job_effective"] >= usage["job_limit"]:
            error = BudgetExceeded("工作預算已達上限")
            error.code = "BUDGET-001"
            raise error
