"""OpenAI-only monetary accounting, including in-flight and retained receipts."""
from datetime import datetime

from inktime.app.domain.analysis.billing_cycle import billing_cycle


class OpenAIBudgetExceeded(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.code = "BUDGET-OPENAI-" + kind.upper()
        self.pause_kind = kind
        self.not_sent = True


class OpenAIBudget:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    def enabled(self):
        return bool(self.settings.get("analysis.complementary_enabled", False))

    def is_openai(self, provider_id):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT 1 FROM providers WHERE id=? AND (kind='openai' OR base_url LIKE 'https://api.openai.com/%')",
                (provider_id,),
            ).fetchone() is not None

    def snapshot(self, now: datetime | None = None) -> dict:
        period = billing_cycle(str(self.settings.get("openai_budget.cycle_anchor", "2026-01-01")),
                               int(self.settings.get("openai_budget.cycle_months", 1)),
                               str(self.settings.get("general.timezone", "Asia/Taipei")), now)
        reserve = max(.01, float(self.settings.get("budget.unknown_request_reserve", .25)))
        with self.database.session() as connection:
            def total(start, end):
                row = connection.execute(
                    "SELECT COALESCE(SUM(cost),0),COALESCE(SUM(uncertain),0) FROM openai_budget_receipts "
                    "WHERE julianday(started_at)>=julianday(?) AND julianday(started_at)<julianday(?)", (start, end),
                ).fetchone()
                return float(row[0]), int(row[1])
            cycle_cost, cycle_unknown = total(period["start"], period["end"])
            daily_cost, daily_unknown = total(period["day_start"], period["day_end"])
            # Unknown legacy reservations remain conservative across renewals.
            held = float(connection.execute(
                "SELECT COALESCE(SUM(r.amount),0) FROM budget_reservations r WHERE r.state='active' "
                "AND (r.provider_kind IS NULL OR r.provider_kind='openai')"
            ).fetchone()[0])
        limit = float(self.settings.get("openai_budget.cycle_limit", 20))
        stop = float(self.settings.get("openai_budget.stop_percent", 98)) / 100
        effective = cycle_cost + cycle_unknown * reserve + held
        daily_limit = float(self.settings.get("openai_budget.daily_limit", 0))
        return {**period, "known": cycle_cost, "unknown_count": cycle_unknown, "reserved": held,
                "effective": effective, "limit": limit, "stop_at": limit * stop,
                "daily_known": daily_cost, "daily_effective": daily_cost + daily_unknown * reserve + held,
                "daily_limit": daily_limit, "daily_stop_at": daily_limit * stop,
                "remaining": max(0, limit * stop - effective),
                "warning": effective >= limit * float(self.settings.get("openai_budget.warning_percent", 80)) / 100,
                "billing_balance": "需至 OpenAI Billing 查詢"}

    def assert_reserve(self, amount=0):
        state = self.snapshot()
        if state["effective"] + amount > state["stop_at"] or state["effective"] >= state["stop_at"]:
            raise OpenAIBudgetExceeded("cycle", "OpenAI 本期預算已接近暫停值；保留進度等待下期")
        if state["daily_limit"] > 0 and (state["daily_effective"] + amount > state["daily_stop_at"] or state["daily_effective"] >= state["daily_stop_at"]):
            raise OpenAIBudgetExceeded("daily", "OpenAI 每日預算已接近暫停值；保留進度等待下一天")
        return state
