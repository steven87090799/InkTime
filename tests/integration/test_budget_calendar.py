from datetime import datetime

from flask import template_rendered
import pytest

import inktime.app.api.dashboard as dashboard_module
import inktime.app.api.settings as settings_module
import inktime.app.services.budgets as budgets_module
from inktime.app.services.usage_periods import usage_periods
from tests.conftest import create_admin, login


def test_concurrent_paid_reservations_share_atomic_global_budget(app):
    from concurrent.futures import ThreadPoolExecutor
    from inktime.app.services.budgets import BudgetExceeded

    settings = app.extensions["inktime_settings_repository"]
    settings.update_many({"budget.daily_stop": 1}, changed_by="test", source_ip="127.0.0.1")
    budget = app.extensions["inktime_budget_service"]

    def reserve(index):
        try:
            budget.reserve(str(index), 0.6)
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(4))) == 1
    assert budget.snapshot()["reserved"] == pytest.approx(0.6)


def test_paid_vision_overrun_blocks_diagnostic_repair(app):
    from tests.unit.test_provider_contracts import FakeContractProvider
    from inktime.app.services.provider_contracts import run_provider_contract

    settings = app.extensions["inktime_settings_repository"]
    settings.update_many({"budget.daily_stop": 0.05}, changed_by="test", source_ip="127.0.0.1")
    provider = FakeContractProvider(valid=False)
    provider.name = "diagnostic"
    provider.estimate_cost = lambda *_args: 0.01
    result = run_provider_contract(provider, level=3, model="model",
                                   budgets=app.extensions["inktime_budget_service"])
    assert not result["ok"]
    assert len(provider.analyze_calls) == 1
    assert provider.repair_calls == []
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 1


def test_failed_paid_usage_counts_toward_job_budget_before_completion(app):
    from inktime.app.services.budgets import BudgetExceeded

    repository = app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(kind="backup", name="budget test", settings={}, created_by=None)
    database = app.extensions["inktime_database"]
    with database.session() as connection:
        connection.execute("UPDATE jobs SET budget_limit=0.1,spent=0 WHERE id=?", (job_id,))
    from inktime.app.repositories.usage import UsageRepository

    UsageRepository(database).record(
        provider="test", model="test", job_id=job_id, photo_id=None,
        request_type="vision", input_tokens=10, output_tokens=5, cached_tokens=0,
        estimated_cost=None, actual_cost=0.12, started_at=datetime.now().astimezone().isoformat(),
        latency_ms=1, status="failed", cost_source="provider_reported",
    )
    assert repository.get(job_id)["spent"] == pytest.approx(0.12)
    budget = app.extensions["inktime_budget_service"]
    assert budget.snapshot(job_id=job_id)["job_known"] == pytest.approx(0.12)
    with pytest.raises(BudgetExceeded):
        budget.assert_request_allowed(job_id, None)
    with database.session() as connection:
        item_id = connection.execute("SELECT id FROM job_items WHERE job_id=?", (job_id,)).fetchone()[0]
        connection.execute("UPDATE job_items SET status='running' WHERE id=?", (item_id,))
    repository.complete_item(job_id, item_id, {"stage": "completed"}, actual_cost=0.12)
    assert repository.get(job_id)["spent"] == pytest.approx(0.12)


def test_response_with_missing_usage_is_reserved_even_without_byte_metrics(app):
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO api_usage(provider,model,request_type,started_at,status,cost_source,estimated_cost) "
            "VALUES ('test','test','vision',?,'completed','unknown',NULL)",
            (datetime.now().astimezone().isoformat(),),
        )
    snapshot = app.extensions["inktime_budget_service"].snapshot()
    assert snapshot["unknown_count"] == 1
    assert snapshot["daily_effective"] == snapshot["unknown_request_reserve"]


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
