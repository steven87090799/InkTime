"""Missing-only completion, ownership and controls; model calls are mocked."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json

import pytest

from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.repositories.billable_operations import BillableOperationRepository, SeasonalAlreadyProcessedError
from inktime.app.repositories.photo_locations import PhotoLocationRepository
from tests.conftest import create_admin, csrf, login
from tests.integration.test_seasonal_analysis import _photo
from tests.unit.test_analysis_schema import valid_result
from inktime.app.workers.runner import WorkerRunner


def setup_backfill(app):
    service = app.extensions["inktime_analysis_backfill_service"]
    actor = create_admin(app)
    service.configure({"model": "mock/temporary-free", "api_key": "mock-backfill-key", "concurrency": 2, "rpm": 20}, actor)
    service.set_enabled(True, actor)
    return service, actor


def save_result(app, photo_id, job_id=None, **updates):
    result = valid_result(**updates)
    app.extensions["inktime_photo_repository"].save_analysis(
        photo_id, job_id, "single", "old-provider", "old-model", result, json.dumps(result),
        score_kind="semantic", schema_kind="full", analysis_fingerprint="old-prompt-old-model",
    )
    return result


def test_missing_is_content_based_and_exclusions_are_completed_model_answers(app):
    service, _ = setup_backfill(app)
    _photo(app, "complete")
    _photo(app, "copy", same_as="complete")
    _photo(app, "model-excluded")
    _photo(app, "missing")
    _photo(app, "local-excluded")
    save_result(app, "complete")  # Valid result has no landmark field.
    save_result(app, "model-excluded", display_exclusion={"category": "document_record", "confidence": 0.99})
    with service.repository.database.transaction() as connection:
        connection.execute("UPDATE photos SET never_upload=1 WHERE id='local-excluded'")
    state = service.repository.status()
    assert (state["total"], state["completed"], state["missing"], state["excluded"]) == (5, 3, 1, 1)
    job_id = service.ensure()["job_id"]
    assert [item["photo_id"] for item in service.jobs.list_items(job_id)] == ["missing"]


def test_oldest_first_is_bounded_and_same_content_has_one_owner(app):
    service, _ = setup_backfill(app)
    for photo_id, captured, same_as in [("new", "2025-01-01", None), ("old", "1998-01-01", None),
                                        ("old-copy", "1999-01-01", "old"), ("middle", "2001-01-01", None)]:
        _photo(app, photo_id, captured, same_as=same_as)
    first = service.ensure()["job_id"]
    assert service.jobs.get(first)["total_items"] == 1  # Old + identical copy collapse.
    assert service.ensure()["job_id"] == first
    assert service.jobs.list_items(first)[0]["photo_id"] == "old"


def test_concurrent_clicks_cannot_create_duplicate_content_jobs(app):
    service, _ = setup_backfill(app)
    _photo(app, "one")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: service.ensure(), range(2)))
    with service.repository.database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM analysis_backfill_tasks").fetchone()[0] == 1


def test_unknown_requests_and_failed_items_are_not_automatically_resent(app):
    service, _ = setup_backfill(app)
    sha = _photo(app, "unknown")
    BillableOperationRepository(service.repository.database).begin(sha, "unconfirmed-request")
    _photo(app, "failed")
    first = service.ensure()["job_id"]
    item = service.jobs.claim(first, "worker", 1)[0]
    service.jobs.fail_item(first, item["id"], "CONFIG_INVALID", "provider rejected", max_attempts=1, worker_id="worker")
    service.jobs.finalize_if_done(first)
    service.repository.refresh()
    assert service.ensure()["job_id"] is None
    state = service.repository.status()
    assert (state["unknown"], state["failed"], state["missing"]) == (1, 1, 0)


def test_switch_off_preserves_queue_and_drains_claimed_request(app):
    service, actor = setup_backfill(app)
    _photo(app, "first")
    _photo(app, "second")
    job_id = service.ensure()["job_id"]
    item = service.jobs.claim(job_id, "worker", 1)[0]
    before_mode = service.settings.get("analysis.execution_mode")
    before_window = service.settings.get("analysis.seasonal_window_days")
    service.set_enabled(False, actor)
    assert service.jobs.claim(job_id, "other-worker", 1) == []
    save_result(app, item["photo_id"], job_id)
    assert service.jobs.complete_item(job_id, item["id"], {"stage": "single"}, 0, "worker")
    service.jobs.acknowledge_pause(job_id)
    assert service.jobs.get(job_id)["status"] == "paused"
    service.set_enabled(True, actor)
    assert service.ensure()["job_id"] == job_id
    assert len(service.jobs.claim(job_id, "worker", 2)) == 1
    assert (service.settings.get("analysis.execution_mode"), service.settings.get("analysis.seasonal_window_days")) == (before_mode, before_window)


def test_new_older_photo_resets_cursor_after_scan(app):
    service, _ = setup_backfill(app)
    _photo(app, "old", "2000-01-01")
    job_id = service.ensure()["job_id"]
    item = service.jobs.claim(job_id, "worker", 1)[0]
    save_result(app, "old", job_id)
    service.jobs.complete_item(job_id, item["id"], {"stage": "single"}, 0, "worker")
    service.jobs.finalize_if_done(job_id)
    assert service.ensure()["job_id"] is None
    _photo(app, "newly-added-older", "1990-01-01")
    service.repository.scan_completed()
    new_job = service.ensure()["job_id"]
    assert service.jobs.list_items(new_job)[0]["photo_id"] == "newly-added-older"


def test_saved_content_result_survives_move_and_live_analysis_removal(app, monkeypatch):
    service, _ = setup_backfill(app)
    sha = _photo(app, "original")
    save_result(app, "original")
    with service.repository.database.transaction() as connection:
        connection.execute("DELETE FROM photo_analysis WHERE photo_id='original'")
        connection.execute("UPDATE photos SET relative_path='moved/same.jpg' WHERE id='original'")
    _photo(app, "rescanned-copy", same_as="original")
    assert service.repository.covered(sha)
    assert service.repository.status()["completed"] == 2
    assert service.ensure()["job_id"] is None
    monkeypatch.setattr(service.analysis, "analyze_photo", lambda **_: pytest.fail("no second model call"))
    inherited = service.photos.inherit_existing_analysis("rescanned-copy", None, semantic_only=True)
    assert inherited is not None


def test_higher_priority_analysis_preempts_backfill_but_waiting_render_cannot_deadlock_it(app):
    service, _ = setup_backfill(app)
    _photo(app, "backfill")
    job_id = service.ensure()["job_id"]
    render_id = service.jobs.create_maintenance(kind="render", name="waiting for model", settings={}, created_by=None, priority=2)
    service.job_service.start(render_id)
    assert service.can_run(job_id)
    _photo(app, "urgent")
    urgent = service.jobs.create(name="tomorrow", strategy="single", settings={}, photo_ids=["urgent"], created_by=None, priority=1)
    service.job_service.start(urgent)
    assert not service.can_run(job_id)
    assert service.jobs.claim(job_id, "worker", 1) == []


def test_special_api_key_never_enters_daily_route_and_profile_changes_only_unclaimed_items(app):
    service, actor = setup_backfill(app)
    assert service.providers.route_snapshot() == []
    assert service.providers.usable_route_snapshot() == []
    router = service.providers.build_router(service._plan()["provider_route"])
    assert len(router.channels) == 1
    assert router.channels[0].provider.options["free_only"]
    router.close()
    with pytest.raises(ValueError, match="補齊專用"):
        service.providers.build_router([{"provider_id": service.PROVIDER_ID}])
    _photo(app, "photo")
    job_id = service.ensure()["job_id"]
    service.set_enabled(False, actor)
    service.configure({"model": "mock/another-free", "api_key": "rotated-mock", "concurrency": 1, "rpm": 10}, actor)
    service.set_enabled(True, actor)
    assert service.ensure()["job_id"] == job_id
    job = service.jobs.get(job_id)
    assert json.loads(job["analysis_spec_json"])["provider_route"][0]["model"] == "mock/another-free"
    assert "rotated-mock" not in job["analysis_spec_json"]


def test_catalog_price_change_pauses_without_consuming_item(app, monkeypatch):
    service, _ = setup_backfill(app)
    _photo(app, "price-change")
    job_id = service.ensure()["job_id"]
    item = service.jobs.claim(job_id, "worker", 1)[0]
    def reject(**_):
        raise ProviderHTTPError("model now costs money", "BACKFILL-FREE-GUARD", request_started=False, vision_started=False)
    monkeypatch.setattr(service.analysis, "analyze_photo", reject)
    with pytest.raises(ProviderHTTPError) as raised:
        service.analyze_item(photo_id="price-change", job_id=job_id)
    assert raised.value.code == "VLM-005"
    assert service.repository.control()["pause_reason"]
    assert service.jobs.get(job_id)["status"] == "pausing"
    assert service.jobs.defer_item(item["id"], "worker", defer_pending=True, retry_after_seconds=300)
    assert service.jobs.list_items(job_id)[0]["status"] == "pending"


def test_durable_last_boundary_rechecks_completion_even_after_a_plan_change(app):
    service, _ = setup_backfill(app)
    sha = _photo(app, "last-boundary")
    job_id = service.ensure()["job_id"]
    save_result(app, "last-boundary")
    with pytest.raises(SeasonalAlreadyProcessedError):
        BillableOperationRepository(service.repository.database).begin(sha, "different-model", context={"job_id": job_id})


def test_monthly_audit_is_a_local_background_job(app):
    service, _ = setup_backfill(app)
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    job_id = service.enqueue_audit(now)
    assert service.jobs.get(job_id)["kind"] == "analysis_coverage_audit"
    assert service.enqueue_audit(now) is None
    assert app.extensions["inktime_schedule_repository"].get("full_reconcile")["cron"] == "0 4 1 * *"
    service.repository.reconcile(month="2026-10")
    assert service.enqueue_audit(now) is None


def test_missing_page_has_csrf_protection_and_never_returns_key(client, app):
    service, _ = setup_backfill(app)
    service.set_enabled(False, "admin")
    assert client.get("/analysis/missing").status_code in {302, 401}
    login(client)
    page = client.get("/analysis/missing")
    assert page.status_code == 200
    assert "補齊缺少項目" in page.get_data(as_text=True)
    data = client.get("/api/v1/analysis/missing")
    assert b"mock-backfill-key" not in data.data
    assert client.post("/api/v1/analysis/missing/toggle", json={"enabled": True}).status_code == 403
    response = client.post("/api/v1/analysis/missing/toggle", json={"enabled": True}, headers={"X-CSRF-Token": csrf(client)})
    assert response.status_code == 200
    assert response.json["normal_mode"] == "seasonal_ai"


def test_failed_only_retry_uses_new_special_model_and_preserves_successes(app):
    service, actor = setup_backfill(app)
    _photo(app, "success", "2000-01-01")
    _photo(app, "failure", "2000-01-02")
    source = service.ensure()["job_id"]
    items = {item["photo_id"]: item for item in service.jobs.claim(source, "worker", 2)}
    save_result(app, "success", source)
    service.jobs.complete_item(source, items["success"]["id"], {"stage": "single"}, 0, "worker")
    service.jobs.fail_item(source, items["failure"]["id"], "CONFIG_INVALID", "rejected", max_attempts=1, worker_id="worker")
    service.jobs.finalize_if_done(source)
    service.repository.refresh()
    service.set_enabled(False, actor)
    service.configure({"model": "mock/replacement-free", "concurrency": 2, "rpm": 20}, actor)
    service.set_enabled(True, actor)
    result = service.retry_failed(source, actor)
    assert result["affected"] == 1
    assert [row["photo_id"] for row in service.jobs.list_items(result["job_id"])] == ["failure"]
    assert json.loads(service.jobs.get(result["job_id"])["analysis_spec_json"])["provider_route"][0]["model"] == "mock/replacement-free"
    assert service.jobs.get(source)["completed_items"] == 1
    assert service.retry_failed(source, actor)["job_id"] == result["job_id"]


def test_worker_yields_after_each_batch_to_recheck_frame_priorities(app, monkeypatch):
    service, _ = setup_backfill(app)
    for number in range(3):
        _photo(app, f"photo-{number}", f"2000-01-0{number + 1}")
    first = service.ensure()["job_id"]
    def local_result(_app, item, **kwargs):
        save_result(app, item["photo_id"], kwargs["job"]["id"])
        return {"stage": "single", "_actual_cost": 0}
    monkeypatch.setattr("inktime.app.workers.runner._execute_job_item", local_result)
    assert WorkerRunner(app, isolate_local=False).run_once() == 1
    assert service.jobs.get(first)["completed_items"] == 2
    active = service._active_jobs()
    assert len(active) == 1
    assert active[0]["total_items"] == 1
    assert active[0]["completed_items"] == 0


def test_content_reuse_preserves_named_landmark_evidence(app):
    service, _ = setup_backfill(app)
    sha = _photo(app, "with-landmark")
    save_result(app, "with-landmark")
    locations = PhotoLocationRepository(service.repository.database)
    locations.save_landmark("with-landmark", sha, None, None, name="臺北一〇一", confidence=0.99)
    _photo(app, "same-with-landmark", same_as="with-landmark")
    service.photos.inherit_existing_analysis("same-with-landmark", None, semantic_only=True)
    copied = locations.get(service.photos.get_with_path("same-with-landmark"))
    assert copied.landmark == "臺北一〇一"
