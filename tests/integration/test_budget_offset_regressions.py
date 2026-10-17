from datetime import datetime

import pytest

import inktime.app.services.budgets as budgets_module
from inktime.app.services.usage_periods import usage_periods


@pytest.mark.parametrize("started_at", ["2026-10-17T12:00:00+00:00", "2026-10-17T20:00:00+08:00"])
@pytest.mark.parametrize("source,cost,expected", [("unknown", None, 0.25), ("estimated", 0.2, 0.2)])
def test_same_instant_counts_toward_the_same_local_budget_day(app, monkeypatch, started_at, source, cost, expected):
    periods = usage_periods("Asia/Taipei", now=datetime.fromisoformat("2026-10-17T12:30:00+00:00"))
    monkeypatch.setattr(budgets_module, "usage_periods", lambda _zone: periods)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO api_usage(provider,model,request_type,started_at,status,cost_source,estimated_cost) "
            "VALUES ('test','test','vision',?,'completed',?,?)", (started_at, source, cost),
        )
    snapshot = app.extensions["inktime_budget_service"].snapshot()
    assert snapshot["daily_effective"] == pytest.approx(expected), {
        "same_instant": started_at, "daily_effective": snapshot["daily_effective"], "expected": expected,
    }
