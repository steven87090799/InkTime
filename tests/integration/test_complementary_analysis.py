"""Two explicit lanes, cycle accounting and renewal; all calls use mocks."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json

import pytest

from inktime.app.domain.analysis.billing_cycle import billing_cycle
from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.services.openai_budget import OpenAIBudgetExceeded
from tests.conftest import create_admin, csrf, login
from tests.integration.test_analysis_backfill import save_result
from tests.integration.test_openai_direct import save_direct, ResponsesBatch
from tests.integration.test_seasonal_analysis import _photo
from tests.unit.test_batch_analysis_lifecycle import _prepare_photos


def setup_dual(app, **updates):
    service = app.extensions["inktime_complementary_analysis_service"]
    actor = create_admin(app)
    direct_id = save_direct(app, supports_batch=True)
    router_id = service.providers.repository.save({"id": "daily-free", "name": "Daily OpenRouter", "kind": "openrouter",
        "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/free", "api_key": "mock-only-router-key"}, actor)
    service.configure({"analysis.complementary_enabled": True, "analysis.openai_provider_id": direct_id,
        "analysis.daily_openrouter_provider_id": router_id, "analysis.openai_backfill_enabled": True,
        "analysis.openai_backfill_chunk": 2, **updates}, actor)
    return service, actor


def record_cost(app, cost, *, provider_id="direct-test", started_at=None):
    return app.extensions["inktime_usage_repository"].record(provider="mock", provider_id=provider_id, model="gpt-6-luna",
        job_id=None, photo_id=None, request_type="test", input_tokens=100, output_tokens=10, cached_tokens=0,
        estimated_cost=cost, actual_cost=None, started_at=started_at or datetime.now().astimezone().isoformat(),
        latency_ms=1, status="completed", cost_source="estimated" if cost is not None else "unknown")


def test_billing_cycle_clamps_month_end_without_drifting_and_supports_yearly():
    feb = billing_cycle("2026-01-31", 1, "Asia/Taipei", datetime.fromisoformat("2026-02-28T00:00:00+08:00"))
    assert feb["start"].startswith("2026-02-27T16:00") and feb["end"].startswith("2026-03-30T16:00")
    leap = billing_cycle("2024-02-29", 12, "Asia/Taipei", datetime.fromisoformat("2027-02-28T12:00:00+08:00"))
    assert leap["end"].startswith("2028-02-28T16:00")
    before = billing_cycle("2026-10-15", 1, "Asia/Taipei", datetime.fromisoformat("2026-10-14T23:59:59+08:00"))
    assert before["end"].startswith("2026-10-14T16:00")


def test_explicit_routes_free_price_fence_and_original_routes_are_preserved(app):
    service, _ = setup_dual(app)
    daily = app.extensions["inktime_seasonal_analysis_service"]
    assert daily.window() == 0
    plan = daily._plan()
    assert len(plan["provider_route"]) == 1 and plan["provider_route"][0]["provider_id"] == "daily-free"
    assert plan["provider_route"][0]["complementary_lane"] == "openrouter_daily"
    router = service.providers.build_router(plan["provider_route"])
    try:
        assert len(router.channels) == 1
        assert router.channels[0].provider.options["free_only"] is True
        assert router.channels[0].provider.options["max_price"]["image"] == 0
    finally:
        router.close()
    assert not service.providers.repository.get("daily-free")["options"].get("free_only")
    assert service.settings.get("analysis.seasonal_window_days") == 5
    assert app.extensions["inktime_analysis_backfill_service"].ensure()["reason"] == "free_quota_reserved_for_tomorrow"


def test_oldest_first_preserves_completion_across_providers_and_restarts(app):
    service, _ = setup_dual(app)
    _photo(app, "done", "1990-01-01")
    _photo(app, "done-copy", "1991-01-01", same_as="done")
    save_result(app, "done")
    _photo(app, "new", "2026-10-30")
    _photo(app, "old", "1999-01-01")
    job_id = service.ensure()["job_id"]
    assert {row["photo_id"] for row in service.jobs.list_items(job_id)} == {"old", "new"}
    assert service.jobs.claim(job_id, "test-worker", 1)[0]["photo_id"] == "old"
    assert service.ensure()["job_id"] == job_id
    plan = json.loads(service.jobs.get(job_id)["analysis_spec_json"])
    assert plan["model"] == "gpt-6-luna" and len(plan["provider_route"]) == 1
    assert plan["provider_route"][0]["provider_id"] == "direct-test"


def test_concurrent_enqueues_and_budget_reservations_cannot_double_spend(app):
    service, _ = setup_dual(app, **{"openai_budget.cycle_limit": 1, "openai_budget.stop_percent": 100})
    _photo(app, "one")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: service.ensure(), range(2)))
    assert len(service.repository.active_jobs()) == 1

    def reserve(name):
        try:
            service.budgets.reserve(name, .6, provider_id="direct-test")
            return True
        except OpenAIBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ["left", "right"])) == [False, True]
    assert service.budgets.openai.snapshot()["reserved"] == pytest.approx(.6)


def test_retained_receipts_survive_usage_cleanup_and_reconciliation_replaces_cost(app):
    service, _ = setup_dual(app)
    usage_id = record_cost(app, None)
    assert service.budgets.openai.snapshot()["unknown_count"] == 1
    with service.repository.database.transaction() as connection:
        connection.execute("UPDATE api_usage SET estimated_cost=.75,cost_source='estimated' WHERE id=?", (usage_id,))
    record_cost(app, 9, provider_id="daily-free")
    assert service.budgets.openai.snapshot()["known"] == .75
    with service.repository.database.transaction() as connection:
        connection.execute("DELETE FROM api_usage")
    state = service.budgets.openai.snapshot()
    assert state["known"] == .75 and state["unknown_count"] == 0


def test_openai_stop_does_not_block_free_daily_reservations(app):
    service, _ = setup_dual(app, **{"openai_budget.cycle_limit": 1})
    record_cost(app, 1)
    _photo(app, "tomorrow", "2000-12-31")
    seasonal = app.extensions["inktime_seasonal_analysis_service"]
    job_id = seasonal.ensure(datetime.fromisoformat("2026-12-31").date())["job_id"]
    service.budgets.assert_request_allowed(job_id, "tomorrow")
    service.budgets.reserve("free", 0, job_id=job_id, photo_id="tomorrow", provider_id="daily-free")
    with pytest.raises(OpenAIBudgetExceeded):
        service.budgets.reserve("paid", .001, provider_id="direct-test")


def test_late_batch_cost_is_counted_on_receipt_and_unknown_holds_survive_renewal(app):
    service, _ = setup_dual(app)
    record_cost(app, .7, started_at="2025-01-01T00:00:00+00:00")
    assert service.budgets.openai.snapshot()["known"] == .7
    service.budgets.reserve("ambiguous", .4, provider_id="direct-test")
    future = service.budgets.openai.snapshot(now=datetime.fromisoformat("2028-01-01T12:00:00+08:00"))
    assert future["known"] == 0 and future["reserved"] == .4


def test_old_daily_queue_is_released_without_retrying_sent_or_unknown_photos(app):
    service, _ = setup_dual(app)
    _photo(app, "yesterday", "2020-10-21")
    seasonal = app.extensions["inktime_seasonal_analysis_service"]
    job_id = seasonal.ensure(datetime.fromisoformat("2026-10-21").date())["job_id"]
    seasonal.ensure(datetime.fromisoformat("2026-10-22").date())
    old = service.jobs.list_items(job_id)[0]
    assert old["status"] == "cancelled" and old["error_code"] == "DAILY-DEFERRED"
    assert service.repository.candidates(5)[0]["id"] == "yesterday"


def test_daily_queues_only_exact_tomorrow_for_all_years_and_hands_off_unpaid_items(app):
    service, _ = setup_dual(app)
    _photo(app, "tomorrow-old", "2000-12-31")
    _photo(app, "tomorrow-new", "2020-12-31")
    _photo(app, "adjacent", "2020-12-30")
    background = service.ensure()["job_id"]
    service.jobs.transition(background, {"running"}, "budget_exceeded", "test")
    daily = app.extensions["inktime_seasonal_analysis_service"].ensure(datetime.fromisoformat("2026-12-31").date())["job_id"]
    assert {row["photo_id"] for row in service.jobs.list_items(daily)} == {"tomorrow-old", "tomorrow-new"}
    assert any(row["error_code"] == "DAILY-HANDOFF" for row in service.jobs.list_items(background))


def test_budget_renewal_resumes_only_automatic_pause_and_keeps_unknown_reservations(app, monkeypatch):
    service, actor = setup_dual(app)
    _photo(app, "old")
    job_id = service.ensure()["job_id"]
    state = service.budgets.openai.snapshot()
    service._pause_for_error(OpenAIBudgetExceeded("cycle", "budget reached"), job_id)
    service.jobs.transition(job_id, {"running"}, "budget_exceeded", "test")
    original = service.budgets.openai.snapshot
    monkeypatch.setattr(service.budgets.openai, "snapshot", lambda: {**original(), "start": "next-cycle"})
    assert service.ensure()["job_id"] == job_id
    assert service.jobs.get(job_id)["status"] == "running"
    service.job_service.pause(job_id)
    assert service.repository.control()["pause_kind"] == "manual"
    monkeypatch.setattr(service.budgets.openai, "snapshot", lambda: {**original(), "start": "another-cycle"})
    assert service.ensure()["job_id"] is None
    assert service.jobs.get(job_id)["status"] == "paused"
    service.resume(actor)
    assert service.jobs.get(job_id)["status"] == "running"
    assert state["start"] != "next-cycle"


def test_credit_pause_allows_only_one_automatic_resume_per_cycle(app, monkeypatch):
    service, _ = setup_dual(app, **{"analysis.openai_backfill_concurrency": 3})
    _photo(app, "credit")
    job_id = service.ensure()["job_id"]
    rejected = ProviderHTTPError("credit exhausted", "API_CREDIT_EXHAUSTED", http_status=429)
    service._pause_for_error(rejected, job_id)
    service.jobs.request_pause(job_id)
    assert service.ensure()["job_id"] is None
    original = service.budgets.openai.snapshot
    monkeypatch.setattr(service.budgets.openai, "snapshot", lambda: {**original(), "start": "next-cycle"})
    assert service.ensure()["job_id"] == job_id
    assert json.loads(service.jobs.get(job_id)["settings_json"])["concurrency"] == 1
    service._pause_for_error(rejected, job_id)
    service.jobs.request_pause(job_id)
    assert service.ensure()["job_id"] is None
    assert service.repository.control()["credit_attempt_cycle"] == "next-cycle"


def test_custom_settings_api_requires_csrf_validates_dates_and_never_exposes_keys(app, client):
    service, _ = setup_dual(app)
    login(client)
    path = "/api/v1/analysis/complementary/settings"
    assert client.post(path, json={"openai_budget.cycle_limit": 12}).status_code == 403
    headers = {"X-CSRF-Token": csrf(client)}
    assert client.post(path, json={"openai_budget.cycle_anchor": "2026-02-30"}, headers=headers).status_code == 409
    assert client.post(path, json={"openai_budget.cycle_limit": 12.5, "openai_budget.cycle_anchor": "2026-10-17",
        "openai_budget.cycle_months": 2, "openai_budget.stop_percent": 95, "openai_budget.daily_limit": 2}, headers=headers).status_code == 200
    page = client.get("/analysis/missing").get_data(as_text=True)
    assert 'name="openai_budget.cycle_limit"' in page and "2026-10-17" in page
    assert "sk-native-private" not in page and "mock-only-router-key" not in page
    assert client.post(path, json={"analysis.openai_backfill_mode": "batch"}, headers=headers).status_code == 409
    assert service.settings.get("analysis.openai_backfill_mode") == "realtime"


def test_automatic_batch_has_explicit_consent_and_preserves_credit_rejection_for_renewal(app, tmp_path, monkeypatch):
    service, actor = setup_dual(app)
    _prepare_photos(app, tmp_path, 1)
    batch, fake = service.batch, ResponsesBatch()
    monkeypatch.setattr(batch, "_provider", lambda *args, **kwargs: fake)
    service.configure({"analysis.openai_backfill_mode": "batch", "analysis.openai_batch_consent": True}, actor)
    counts = {"calls": 0}
    original_create = fake.create_batch
    def no_credit(*args, **kwargs):
        counts["calls"] += 1
        raise ProviderHTTPError("credit exhausted", "API_CREDIT_EXHAUSTED", http_status=429)
    monkeypatch.setattr(fake, "create_batch", no_credit)
    job_id = service.ensure()["job_id"]
    assert counts["calls"] == 1
    assert service.repository.control()["pause_kind"] == "credit"
    assert service.jobs.get(job_id)["status"] == "paused"
    assert service.ensure()["job_id"] is None and counts["calls"] == 1
    assert all(item["status"] == "pending" for item in service.jobs.list_items(job_id))
    original_snapshot = service.budgets.openai.snapshot
    monkeypatch.setattr(service.budgets.openai, "snapshot", lambda: {**original_snapshot(), "start": "next-cycle"})
    monkeypatch.setattr(fake, "create_batch", original_create)
    assert service.ensure()["job_id"] == job_id
    batch.poll_due()
    with service.repository.database.session() as connection:
        remote = connection.execute("SELECT remote_batch_id FROM analysis_batches WHERE job_id=?", (job_id,)).fetchone()[0]
    assert remote is not None


def test_automatic_batch_can_keep_only_the_shared_prefilter_candidates(app, tmp_path, monkeypatch):
    service, actor = setup_dual(app, **{"analysis.openai_backfill_chunk": 3})
    photo_ids = _prepare_photos(app, tmp_path, 3)
    fake = ResponsesBatch()
    monkeypatch.setattr(service.batch, "_provider", lambda *args, **kwargs: fake)
    original = service.analysis.prefilter_snapshot
    monkeypatch.setattr(service.analysis, "prefilter_snapshot", lambda photo, **kwargs:
        {**original(photo, **kwargs), "excluded": True} if photo["id"] == photo_ids[0] else original(photo, **kwargs))
    service.configure({"analysis.openai_backfill_mode": "batch", "analysis.openai_batch_consent": True}, actor)
    result = service.ensure()
    assert result["reason"] == "batch_submitted"
    assert service.jobs.get(result["job_id"])["total_items"] == 2


def test_invalid_key_pauses_the_lane_until_manual_review(app, monkeypatch):
    service, _ = setup_dual(app)
    _photo(app, "invalid-key")
    job_id = service.ensure()["job_id"]
    def reject(**kwargs):
        raise ProviderHTTPError("invalid key", "AUTH_REQUIRED", http_status=401)
    monkeypatch.setattr(service.analysis, "analyze_photo", reject)
    with pytest.raises(ProviderHTTPError):
        service.analyze_item(photo_id="invalid-key", job_id=job_id)
    assert service.repository.control()["pause_kind"] == "review"
    assert service.ensure()["job_id"] is None


def test_switching_from_old_free_backfill_preserves_history_without_holding_unpaid_photos(app):
    service, actor = setup_dual(app)
    service.settings.update("analysis.complementary_enabled", False, changed_by=actor, source_ip="test")
    old = app.extensions["inktime_analysis_backfill_service"]
    old.configure({"model": "openrouter/free", "api_key": "mock-old-free", "concurrency": 2, "rpm": 20}, actor)
    old.set_enabled(True, actor)
    _photo(app, "old-free-queued")
    old_job = old.ensure()["job_id"]
    service.settings.update("analysis.complementary_enabled", True, changed_by=actor, source_ip="test")
    assert old.ensure()["reason"] == "free_quota_reserved_for_tomorrow"
    assert old.jobs.list_items(old_job)[0]["error_code"] == "DAILY-DEFERRED"
    new_job = service.ensure()["job_id"]
    assert service.jobs.list_items(new_job)[0]["photo_id"] == "old-free-queued"
