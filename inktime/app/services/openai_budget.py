"""OpenAI-only monetary accounting, including in-flight and retained receipts."""
from datetime import datetime

from inktime.app.domain.analysis.billing_cycle import billing_cycle
from inktime.app.repositories.settings import SETTING_DEFINITIONS

MONTHLY_TOTAL = 40.0
ACCOUNT_LIMIT = 20.0


class OpenAIBudgetExceeded(RuntimeError):
    def __init__(self, kind: str, message: str, *, required_amount=None):
        super().__init__(message)
        self.code = "BUDGET-OPENAI-" + kind.upper()
        self.pause_kind = kind
        self.not_sent = True
        self.required_amount = required_amount


class OpenAIBudget:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    def enabled(self):
        return self.settings.get_many(["analysis.complementary_enabled"], fresh=True)["analysis.complementary_enabled"] is True

    def is_openai(self, provider_id):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT 1 FROM providers WHERE id=? AND (kind='openai' OR base_url LIKE 'https://api.openai.com/%')",
                (provider_id,),
            ).fetchone() is not None

    def snapshot(self, now: datetime | None = None) -> dict:
        keys = [
            "analysis.openai_backfill_policy", "analysis.openai_provider_id", "analysis.openai_secondary_provider_id",
            "openai_budget.cycle_anchor", "openai_budget.secondary_anchor", "openai_budget.cycle_months", "general.timezone", "budget.unknown_request_reserve",
            "openai_budget.cycle_limit", "openai_budget.stop_percent", "openai_budget.daily_limit", "openai_budget.warning_percent",
        ]
        cfg = self.settings.get_many(keys, defaults={key: SETTING_DEFINITIONS[key]["default"] for key in keys}, fresh=True)
        policy = cfg["analysis.openai_backfill_policy"]
        monthly = policy in {"monthly_shared", "monthly_split"}
        period = billing_cycle(cfg["openai_budget.cycle_anchor"], 1 if monthly else cfg["openai_budget.cycle_months"],
                               cfg["general.timezone"], now)
        reserve = max(.01, float(cfg["budget.unknown_request_reserve"]))
        with self.database.session() as connection:
            cycle = {row[0]: (float(row[1]), int(row[2])) for row in connection.execute(
                "SELECT provider_id,COALESCE(SUM(cost),0),SUM(uncertain) FROM openai_budget_receipts "
                "WHERE julianday(started_at)>=julianday(?) AND julianday(started_at)<julianday(?) GROUP BY provider_id",
                (period["start"], period["end"]))}
            daily_cost, daily_unknown = connection.execute(
                "SELECT COALESCE(SUM(cost),0),COALESCE(SUM(uncertain),0) FROM openai_budget_receipts "
                "WHERE julianday(started_at)>=julianday(?) AND julianday(started_at)<julianday(?)",
                (period["day_start"], period["day_end"])).fetchone()
            reservations = dict(connection.execute(
                "SELECT provider_id,SUM(amount) FROM budget_reservations WHERE state='active' "
                "AND (provider_kind IS NULL OR provider_kind='openai') GROUP BY provider_id"))
            holds = dict(connection.execute("SELECT provider_id,cycle_start FROM openai_account_holds"))
            secondary_period = period
            secondary_id = cfg["analysis.openai_secondary_provider_id"]
            account_costs = dict(cycle)
            if policy == "monthly_split" and cfg["openai_budget.secondary_anchor"]:
                secondary_period = billing_cycle(cfg["openai_budget.secondary_anchor"], 1, cfg["general.timezone"], now)
                account_costs[secondary_id] = tuple(connection.execute(
                    "SELECT COALESCE(SUM(cost),0),COALESCE(SUM(uncertain),0) FROM openai_budget_receipts "
                    "WHERE provider_id=? AND julianday(started_at)>=julianday(?) AND julianday(started_at)<julianday(?)",
                    (secondary_id, secondary_period["start"], secondary_period["end"])).fetchone())
            adjustments = {(row[0], row[1]): row[2] for row in connection.execute(
                "SELECT account,cycle_start,untracked_usd FROM openai_credit_adjustments WHERE cycle_start IN (?,?)",
                (period["start"], secondary_period["start"]))}
        cycle_cost = sum(cost for cost, _ in cycle.values())
        cycle_unknown = sum(unknown for _, unknown in cycle.values())
        held = sum(reservations.values())
        limit = MONTHLY_TOTAL if monthly else float(cfg["openai_budget.cycle_limit"])
        stop = 1 if monthly else float(cfg["openai_budget.stop_percent"]) / 100
        daily_limit = 0 if monthly else float(cfg["openai_budget.daily_limit"])
        accounts = {}
        if policy == "monthly_split":
            for provider_id in (cfg["analysis.openai_provider_id"], cfg["analysis.openai_secondary_provider_id"]):
                if not provider_id:
                    continue
                account_period = secondary_period if provider_id == secondary_id else period
                known, unknown = account_costs.get(provider_id, (0, 0))
                adjustment = adjustments.get((provider_id, account_period["start"]), 0)
                # An unidentified old reservation cannot be assigned safely to either account.
                reserved = reservations.get(provider_id, 0) + reservations.get(None, 0)
                accounts[provider_id] = {
                    "start": account_period["start"], "end": account_period["end"],
                    "known": known, "reserved": reserved, "unknown_count": unknown, "untracked": adjustment,
                    "remaining": max(0, ACCOUNT_LIMIT - known - unknown * reserve - reserved - adjustment),
                    "held": holds.get(provider_id) == account_period["start"],
                    "renewal_probe": provider_id in holds and holds[provider_id] != account_period["start"] and known == unknown == 0,
                }
        # In split mode each grant has its own renewal and adjustment. The
        # installation-wide monthly cap still includes every native OpenAI receipt.
        untracked = adjustments.get(("", period["start"]), 0) if policy == "monthly_shared" else 0
        effective = cycle_cost + cycle_unknown * reserve + held + untracked
        remaining = max(0, limit * stop - effective)
        if accounts:
            remaining = min(remaining, sum(a["remaining"] for a in accounts.values() if not a["held"]))
        return {**period, "policy": policy, "accounts": accounts, "known": cycle_cost, "unknown_count": cycle_unknown, "reserved": held,
                "untracked": untracked, "effective": effective, "limit": limit, "stop_at": limit * stop,
                "daily_known": daily_cost, "daily_effective": daily_cost + daily_unknown * reserve + held,
                "daily_limit": daily_limit, "daily_stop_at": daily_limit * stop,
                "remaining": remaining,
                "warning": (limit - remaining if monthly else effective) >= limit * float(cfg["openai_budget.warning_percent"]) / 100,
                "billing_balance": "需至 OpenAI Billing 查詢"}

    def record_balance(self, account, cycle_start, remaining):
        if type(remaining) not in {int, float} or not 0 <= remaining <= MONTHLY_TOTAL:
            raise ValueError("請填入 Billing 顯示的贈額餘額")
        with self.database.transaction() as connection:
            state = self.snapshot()
            if state["policy"] == "monthly_shared" and account == "":
                current, limit = state, MONTHLY_TOTAL
            elif state["policy"] == "monthly_split" and isinstance(account, str) and account in state["accounts"]:
                current, limit = state["accounts"][account], ACCOUNT_LIMIT
            else:
                raise ValueError("補齊模式或帳戶已變更，請重新整理")
            if cycle_start != current["start"] or not 0 <= remaining <= limit:
                raise ValueError("請核對本期補發日與贈額金額，再重新儲存")
            # The billing balance already reflects known local usage. Only
            # the difference is extra spending or a prorated grant.
            untracked = max(0, limit - remaining - current["known"])
            connection.execute("INSERT INTO openai_credit_adjustments(account,cycle_start,untracked_usd) VALUES (?,?,?) "
                               "ON CONFLICT(account,cycle_start) DO UPDATE SET untracked_usd=excluded.untracked_usd",
                               (account, cycle_start, untracked))

    def assert_reserve(self, amount=0, *, provider_id=None):
        state = self.snapshot()
        if state["effective"] + amount > state["stop_at"] or state["effective"] >= state["stop_at"]:
            raise OpenAIBudgetExceeded("cycle", "OpenAI 本期預算已接近暫停值；保留進度等待下期")
        if provider_id is None and state.get("policy") == "monthly_split" and state["remaining"] <= 0:
            raise OpenAIBudgetExceeded("cycle", "兩個 OpenAI 帳戶的本期可用額度已用盡")
        if state["daily_limit"] > 0 and (state["daily_effective"] + amount > state["daily_stop_at"] or state["daily_effective"] >= state["daily_stop_at"]):
            raise OpenAIBudgetExceeded("daily", "OpenAI 每日預算已接近暫停值；保留進度等待下一天")
        if provider_id and state.get("policy") == "monthly_split":
            account = state["accounts"].get(provider_id)
            if account is None:
                raise OpenAIBudgetExceeded("review", "每月分帳模式只允許兩個已指定的 OpenAI 帳戶")
            if account["held"] or account["remaining"] <= 0 or amount > account["remaining"]:
                raise OpenAIBudgetExceeded("account", "此 OpenAI 帳戶的本月額度不足以預留下一次請求",
                                           required_amount=amount)
        return state
