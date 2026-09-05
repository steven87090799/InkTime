from datetime import datetime

from flask import template_rendered
import pytest

import inktime.app.api.dashboard as dashboard_module
import inktime.app.api.settings as settings_module
import inktime.app.services.budgets as budgets_module
from inktime.app.services.usage_periods import usage_periods
from tests.conftest import create_admin, login


def test_budget_dashboard_and_cost_page_share_local_month_boundary(app, client, monkeypatch):
    periods = usage_periods("Asia/Taipei", now=datetime.fromisoformat("2026-08-31T18:00:00+00:00"))
    for module in (dashboard_module, settings_module, budgets_module):
        monkeypatch.setattr(module, "usage_periods", lambda _zone: periods)
    with app.extensions["inktime_database"].session() as connection:
        for started_at, cost in (
            ("2026-08-31T15:59:59+00:00", 100),
            ("2026-08-31T16:00:00+00:00", 2),
            ("2026-09-01T16:00:00+00:00", 4),
            ("2026-09-30T16:00:00+00:00", 200),
        ):
            connection.execute(
                "INSERT INTO api_usage(provider,model,request_type,started_at,status,cost_source,"
                "estimated_cost,input_tokens,output_tokens) VALUES ('test','test','vision',?,'success',"
                "'estimated',?,10,5)", (started_at, cost),
            )
        connection.execute(
            "INSERT INTO api_usage(provider,model,request_type,started_at,status,cost_source,image_bytes) "
            "VALUES ('test','test','vision',?,'failed','unknown',1)", (periods["day_start"],),
        )
    snapshot = app.extensions["inktime_budget_service"].snapshot()
    assert snapshot["daily_known"] == 2
    assert snapshot["monthly_known"] == 6
    assert snapshot["daily_unknown_count"] == snapshot["monthly_unknown_count"] == 1
    assert snapshot["daily_effective"] == pytest.approx(2 + snapshot["unknown_request_reserve"])

    create_admin(app)
    login(client)
    rendered = {}

    def capture(_sender, template, context, **_extra):
        rendered[template.name] = context

    with template_rendered.connected_to(capture, app):
        assert client.get("/dashboard").status_code == 200
        assert client.get("/costs").status_code == 200
    counts = rendered["dashboard.html"]["counts"]
    summary = rendered["costs.html"]["summary"]
    assert counts["today_tokens"] == 15
    assert counts["month_cost"] == summary["month"] == snapshot["monthly_known"]
    assert summary["today"] == snapshot["daily_known"]
    assert summary["today_unknown_count"] == counts["month_unknown_count"] == 1
