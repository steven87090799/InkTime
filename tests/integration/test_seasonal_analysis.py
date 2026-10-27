from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import hashlib
import json

import pytest

from inktime.app.domain.analysis.seasonal import seasonal_month_days
from inktime.app.workers.job_worker import BoundedJobWorker
from tests.conftest import create_admin, csrf, login
from tests.unit.test_analysis_schema import valid_result


def test_default_profile_uses_next_day_weekly_slots_and_unlimited_analysis(app):
    settings = app.extensions["inktime_settings_repository"]
    expected = {
        "analysis.execution_mode": "seasonal_ai",
        "analysis.ai_mode": "on_demand",
        "analysis.prepare_ahead_days": 1,
        "analysis.require_delivery_ack": True,
        "analysis.seasonal_window_days": 5,
        "analysis.ai_daily_photo_limit": 0,
        "analysis.ai_monthly_photo_limit": 0,
        "analysis.side_caption_max_chars": 16,
        "display.weekly_schedule_enabled": True,
    }
    assert {key: settings.get(key) for key in expected} == expected
    schedules = app.extensions["inktime_offline_schedule_repository"]
    assert schedules.times_for_date(date(2026, 10, 26), ["01:00"]) == [
        "08:00", "18:00", "20:00", "22:00",
    ]
    assert schedules.times_for_date(date(2026, 10, 31), ["01:00"]) == [
        "08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00",
    ]
    # Starting after 08:00 still requires today's delivery evidence.
    service = app.extensions["inktime_seasonal_analysis_service"]
    assert service.preparation_target(datetime.fromisoformat("2026-10-27T08:01:00+08:00")) is None


def _setup(app, monkeypatch):
    service = app.extensions["inktime_seasonal_analysis_service"]
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.execution_mode", "seasonal_ai", changed_by="test", source_ip="127.0.0.1")
    monkeypatch.setattr(service, "today", lambda: date(2026, 10, 22))
    monkeypatch.setattr(service, "preparation_target", lambda now, **kwargs: date(2026, 10, 22))
    plan = app.extensions["inktime_analysis_service"].build_plan(
        strategy="single", provider_route=[],
        scoring_profile_id=str(app.extensions["inktime_scoring_repository"].current()["id"]),
    )
    monkeypatch.setattr(service, "_plan", lambda: plan)
    return service


def _photo(app, photo_id, captured="2020-10-22", *, same_as=None):
    content = hashlib.sha256((same_as or photo_id).encode()).hexdigest()
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO libraries(id,name,root_path,created_at,updated_at) "
            "VALUES ('seasonal','Seasonal','/photos','2026-01-01','2026-01-01')"
        )
        connection.execute(
            """INSERT INTO photos(id,library_id,relative_path,sha256,status,eligible,lifecycle_status,
                local_features_status,local_candidate_score,captured_at,captured_date,captured_month_day,
                capture_date_status,created_at,updated_at)
                VALUES (?,'seasonal',?,?,'ready',1,'active','complete',75,?,?,?,'valid','2026-01-01','2026-01-01')""",
            (photo_id, f"{photo_id}.jpg", content, captured, captured, captured[5:] if captured else None),
        )
    return content


def _tasks(service):
    with service.repository.database.session() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM seasonal_analysis_tasks ORDER BY id")]


def _fail(service, job_id, *, ambiguous=False):
    item = service.jobs.claim(job_id, "test-worker", 1)[0]
    if ambiguous:
        from inktime.app.repositories.billable_operations import BillableOperationRepository
        task = _tasks(service)[-1]
        BillableOperationRepository(service.repository.database).begin(task["content_sha256"], "unknown-request")
    service.jobs.fail_item(job_id, item["id"], "VLM-UNRECONCILED" if ambiguous else "TEST-FAILURE",
                           "test failure", max_attempts=1, worker_id="test-worker")
    service.jobs.finalize_if_done(job_id)
    service.repository.refresh()


@pytest.mark.parametrize("target,window,expected", [
    (date(2026, 1, 1), 1, {"12-31", "01-01", "01-02"}),
    (date(2026, 2, 28), 1, {"02-27", "02-28", "02-29"}),
    (date(2028, 2, 29), 1, {"02-28", "02-29", "03-01"}),
])
def test_window_includes_year_boundary_and_leap_day(target, window, expected):
    assert set(seasonal_month_days(target, window)) == expected


def test_overlapping_windows_duplicates_and_model_upgrades_do_not_requeue(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "today")
    _photo(app, "copy", same_as="today")
    _photo(app, "other-season", "2020-04-22")
    first = service.ensure()
    assert first["waiting"]
    assert service.jobs.get(first["job_id"])["total_items"] == 1
    assert len(_tasks(service)) == 1
    monkeypatch.setattr(service, "_plan", lambda: pytest.fail("model change must not create another job"))
    assert service.ensure(date(2026, 10, 23))["job_id"] is None
    assert len(_tasks(service)) == 1


def test_new_out_of_season_photos_wait_until_display_demand(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "spring", "2019-04-22")
    assert service.ensure()["waiting"] is False
    assert not _tasks(service)
    assert service.ensure(date(2027, 4, 22))["job_id"]
    assert len(_tasks(service)) == 1


def test_large_window_is_fully_queued_in_bounded_chunks(app, monkeypatch):
    service = _setup(app, monkeypatch)
    monkeypatch.setattr(service, "CHUNK_SIZE", 2)
    for index in range(5):
        _photo(app, f"bounded-{index}")
    job_ids = [service.ensure()["job_id"] for _ in range(3)]
    assert [service.jobs.get(job_id)["total_items"] for job_id in job_ids] == [2, 2, 1]
    assert len(_tasks(service)) == 5
    assert service.ensure()["job_id"] is None


def test_existing_model_data_is_adopted_without_loading_a_new_model(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "historical")
    with service.repository.database.transaction() as connection:
        connection.execute(
            "INSERT INTO photo_analysis(photo_id,schema_version,stage,provider,model,raw_json,score_kind,created_at) "
            "VALUES ('historical',4,'single','historical-provider','older-model','{}','semantic','2020-10-22')"
        )
    monkeypatch.setattr(service, "_plan", lambda: pytest.fail("model upgrade must not invalidate history"))
    assert service.ensure()["waiting"] is False
    task = _tasks(service)[0]
    assert task["status"] == "completed"
    assert task["job_id"] is None
    assert json.loads(task["analysis_snapshot_json"])["model"] == "older-model"


def test_concurrent_demands_have_one_durable_owner(app, monkeypatch):
    service = _setup(app, monkeypatch)
    for index in range(4):
        _photo(app, f"photo-{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.ensure(), range(2)))
    assert all(result["waiting"] for result in results)
    tasks = _tasks(service)
    assert len(tasks) == 4
    assert len({task["job_id"] for task in tasks}) == 1


@pytest.mark.parametrize("ambiguous", [False, True])
def test_failures_stay_visible_until_explicit_manual_retry(app, monkeypatch, ambiguous):
    service = _setup(app, monkeypatch)
    _photo(app, "failure")
    first = service.ensure()["job_id"]
    _fail(service, first, ambiguous=ambiguous)
    assert _tasks(service)[0]["status"] == ("unknown" if ambiguous else "failed")
    assert service.ensure()["job_id"] is None
    assert service.ensure()["waiting"] is False
    with pytest.raises(ValueError):
        service.retry(_tasks(service)[0]["id"], "operator", accept_possible_charge=False)
    second = service.retry(_tasks(service)[0]["id"], "operator", accept_possible_charge=True)
    assert second != first
    assert len(_tasks(service)) == 2
    assert _tasks(service)[0]["status"] in {"failed", "unknown"}
    with pytest.raises(ValueError):
        service.retry(_tasks(service)[0]["id"], "operator", accept_possible_charge=True)


def test_worker_crash_is_terminal_on_first_attempt(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "crash")
    job_id = service.ensure()["job_id"]
    item = service.jobs.claim(job_id, "crashed-worker", 1)[0]
    with service.repository.database.transaction() as connection:
        connection.execute("UPDATE job_items SET lease_until='2000-01-01' WHERE id=?", (item["id"],))
    assert service.jobs.recover_stale() == 1
    service.repository.refresh()
    assert _tasks(service)[0]["status"] == "failed"
    assert not service.jobs.claim(job_id, "new-worker", 1)
    assert service.ensure()["job_id"] is None


def test_completed_other_model_response_cannot_race_into_another_paid_send(app, monkeypatch):
    from inktime.app.providers.base import ProviderResponse, Usage
    from inktime.app.repositories.billable_operations import BillableOperationRepository, SeasonalAlreadyProcessedError

    service = _setup(app, monkeypatch)
    content = _photo(app, "race")
    job_id = service.ensure()["job_id"]
    operations = BillableOperationRepository(service.repository.database)
    operation_id, _ = operations.begin(content, "previous-model")
    operations.save_response(operation_id, ProviderResponse(json.dumps(valid_result()), Usage(100, 50, 0)))
    operations.finish(operation_id)
    with pytest.raises(SeasonalAlreadyProcessedError):
        operations.begin(content, "new-model", context={"job_id": job_id})
    with service.repository.database.session() as connection:
        assert connection.execute("SELECT count(*) FROM billable_operations").fetchone()[0] == 1


def test_local_analysis_does_not_satisfy_model_coverage(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "local-only")
    result = valid_result()
    service.photos.save_analysis("local-only", None, "local", "local", "local", result,
                                 json.dumps(result), score_kind="local_quality")
    job_id = service.ensure()["job_id"]
    calls = []
    monkeypatch.setattr(service.analysis, "analyze_photo", lambda **kwargs: calls.append(kwargs) or {"stage": "single"})
    assert service.analyze_item(photo_id="local-only", job_id=job_id)["stage"] == "single"
    assert len(calls) == 1


def test_render_waits_and_uses_requested_offline_display_date(app, monkeypatch):
    service = _setup(app, monkeypatch)
    seen = []
    monkeypatch.setattr(service, "ensure", lambda target: seen.append(target) or {"waiting": True})
    assert service.before_render({"offline_prepare": {"target_date": "2027-01-01"}})
    assert seen == [date(2027, 1, 1)]
    assert service.before_render({"photo_ids": ["explicit-photo"]}) is False


def test_seasonal_ranking_uses_the_complete_window_without_another_model_call(app, monkeypatch):
    _setup(app, monkeypatch)
    renderer = app.extensions["inktime_render_service"]
    calls = []
    monkeypatch.setattr(renderer, "_candidate_query", lambda **kwargs: calls.append(kwargs) or [])
    assert renderer.select_candidates_details(target_date=date(2026, 1, 1)) == []
    assert len(calls) == 2
    assert {"12-27", "01-01", "01-06"} <= set(calls[0]["month_days"])
    assert calls[0]["older_only"] is False
    assert calls[1]["month_days"] is None  # Empty window uses the saved high-score fallback.


def test_cancelled_seasonal_task_without_trace_notifies_once(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "cancelled")
    job_id = service.ensure()["job_id"]
    service.job_service.cancel(job_id)
    for _ in range(2):
        service.repository.refresh()
    notifications = app.extensions["inktime_notification_service"]
    assert notifications.unread_analysis()["count"] == 1
    assert json.loads(notifications.list()[0]["details_json"])["photo_id"] == "cancelled"


def test_copy_reuse_failure_preserves_completed_source_result(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "original")
    app.extensions["inktime_photo_repository"].save_analysis(
        "original", None, "single", "test-ai", "model", valid_result(), "{}",
    )
    service.ensure()
    original = _tasks(service)[0]
    _photo(app, "copy", same_as="original")
    def invalid_copy(*args, **kwargs):
        raise ValueError("invalid copy metadata")
    monkeypatch.setattr(service.photos, "inherit_existing_analysis", invalid_copy)
    for _ in range(2):
        assert service.ensure()["job_id"] is None
    after = _tasks(service)[0]
    assert after["status"] == "completed"
    assert after["analysis_snapshot_json"] == original["analysis_snapshot_json"]
    assert app.extensions["inktime_notification_service"].unread_analysis()["count"] == 1


def test_rerun_requires_all_three_server_enforced_stages_and_is_replayable(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "all-year", "2020-04-22")
    preview = service.rerun_preview("operator")
    token, phrase = preview["token"], preview["confirmation_phrase"]
    with pytest.raises(ValueError):
        service.rerun(token, "operator", phrase, True)
    with pytest.raises(ValueError):
        service.rerun_acknowledge(token, "other-operator")
    service.rerun_acknowledge(token, "operator")
    for text, accepted in (("yes", True), (phrase, False)):
        with pytest.raises(ValueError):
            service.rerun(token, "operator", text, accepted)
    job_id = service.rerun(token, "operator", phrase, True)
    assert service.jobs.get(job_id)["force_recompute"] == 1
    assert service.rerun(token, "operator", phrase, True) == job_id
    assert len(_tasks(service)) == 1


def test_rerun_snapshot_change_rolls_back_job_and_confirmation(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "initial")
    preview = service.rerun_preview("operator")
    service.rerun_acknowledge(preview["token"], "operator")
    _photo(app, "arrived-later")
    with pytest.raises(ValueError):
        service.rerun(preview["token"], "operator", preview["confirmation_phrase"], True)
    assert not _tasks(service)
    with service.repository.database.session() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert connection.execute("SELECT stage FROM seasonal_rerun_confirmations").fetchone()[0] == 2


def test_rerun_rejects_changed_model_and_expired_confirmation(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "photo")
    preview = service.rerun_preview("operator")
    service.rerun_acknowledge(preview["token"], "operator")
    plan = {**service._plan(), "model": "new-model"}
    monkeypatch.setattr(service, "_plan", lambda: plan)
    with pytest.raises(ValueError):
        service.rerun(preview["token"], "operator", preview["confirmation_phrase"], True)
    with service.repository.database.transaction() as connection:
        connection.execute("UPDATE seasonal_rerun_confirmations SET expires_at='2000-01-01'")
    with pytest.raises(ValueError):
        service.rerun(preview["token"], "operator", preview["confirmation_phrase"], True)


def test_completed_result_and_snapshot_survive_model_upgrade_and_live_row_removal(app, monkeypatch):
    service = _setup(app, monkeypatch)
    _photo(app, "preserved")
    job_id = service.ensure()["job_id"]
    result = valid_result()
    photos = app.extensions["inktime_photo_repository"]
    photos.save_analysis("preserved", job_id, "single", "mock", "original-model", result, json.dumps(result))
    item = service.jobs.claim(job_id, "test-worker", 1)[0]
    service.jobs.complete_item(job_id, item["id"], {"stage": "single"}, worker_id="test-worker")
    service.jobs.finalize_if_done(job_id)
    service.repository.refresh()
    task = _tasks(service)[0]
    assert task["status"] == "completed"
    assert json.loads(task["analysis_snapshot_json"])["model"] == "original-model"
    with service.repository.database.transaction() as connection:
        connection.execute("DELETE FROM photo_analysis WHERE photo_id='preserved'")
    assert _tasks(service)[0]["analysis_id"] is None
    assert photos.inherit_existing_analysis("preserved", None) is not None
    monkeypatch.setattr(service, "_plan", lambda: pytest.fail("completed results must be reused"))
    assert service.ensure()["job_id"] is None
    assert len(_tasks(service)) == 1


def test_one_image_worker_call_persists_result_and_exact_copies_reuse_it(app, tmp_path, monkeypatch):
    from tests.integration.test_analysis_pipeline import MockProvider, prepare

    service = _setup(app, monkeypatch)
    _, ids, analysis = prepare(app, tmp_path, duplicate=True)
    service.analysis = analysis
    with service.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE photos SET captured_at='2020-10-22',captured_date='2020-10-22',"
            "captured_month_day='10-22',capture_date_status='valid'"
        )
    provider = MockProvider([valid_result()])
    job_id = service.ensure()["job_id"]
    plan = json.loads(service.jobs.get(job_id)["analysis_spec_json"])
    worker = BoundedJobWorker(service.jobs, lambda item: service.analyze_item(
        photo_id=item["photo_id"], job_id=job_id, provider=provider, strategy="single", force_ai=True,
        analysis_plan=plan,
    ), concurrency=1, max_attempts=1)
    worker.run_job(job_id)
    service.repository.refresh()
    assert provider.analyze_calls == 1
    assert provider.repair_calls == 0
    assert _tasks(service)[0]["status"] == "completed"
    assert _tasks(service)[0]["analysis_snapshot_json"]
    assert service.ensure()["job_id"] is None
    with service.repository.database.session() as connection:
        analyzed = {row[0] for row in connection.execute("SELECT DISTINCT photo_id FROM photo_analysis WHERE score_kind='semantic'")}
    assert set(ids) <= analyzed
    assert provider.analyze_calls == 1


def test_api_requires_admin_csrf_and_confirmation_before_queueing(client, app, monkeypatch):
    _setup(app, monkeypatch)
    _photo(app, "api-photo")
    assert client.get("/analysis/seasonal").status_code in {302, 401}
    create_admin(app)
    login(client)
    assert client.get("/analysis/seasonal").status_code == 200
    assert client.post("/api/v1/analysis/seasonal/run", json={}).status_code == 403
    response = client.post("/api/v1/analysis/seasonal/rerun/execute", json={"accept_additional_cost": True}, headers={"X-CSRF-Token": csrf(client)})
    assert response.status_code == 409
    assert not _tasks(app.extensions["inktime_seasonal_analysis_service"])


def test_daily_window_only_adds_new_boundary_day_and_keeps_unselected_candidates(app, monkeypatch):
    service = _setup(app, monkeypatch)
    for day in range(17, 29):
        _photo(app, f"day-{day}", f"2018-10-{day}")
    first = service.ensure(date(2026, 10, 22))["job_id"]
    assert service.jobs.get(first)["total_items"] == 11
    for item in service.jobs.claim(first, "daily-test", 20):
        result = valid_result()
        service.photos.save_analysis(item["photo_id"], first, "single", "mock", "old-model", result, json.dumps(result))
        service.jobs.complete_item(first, item["id"], {"stage": "single"}, worker_id="daily-test")
    service.jobs.finalize_if_done(first)
    assert service.jobs.get(first)["completed_items"] == 11
    second = service.ensure(date(2026, 10, 23))["job_id"]
    assert service.jobs.get(second)["total_items"] == 1
    tasks = _tasks(service)
    assert [t["photo_id"] for t in tasks if t["job_id"] == second] == ["day-28"]
    assert len(tasks) == 12
    candidates = service.repository.candidates(date(2026, 10, 23), 5)
    assert {row["id"] for row in candidates} == {f"day-{day}" for day in range(18, 29)}
    assert service.ensure(date(2026, 10, 23))["job_id"] is None


def test_daily_and_status_queries_are_paged_and_deduplicate_across_page_edges(app, monkeypatch):
    service = _setup(app, monkeypatch)
    repository = service.repository
    monkeypatch.setattr(repository, "PAGE_SIZE", 2)
    monkeypatch.setattr(service, "CHUNK_SIZE", 2)
    for index in range(5):
        _photo(app, f"copy-{index}", same_as="one-content")
    for index in range(4):
        _photo(app, f"distinct-{index}")
    _photo(app, "Screenshot-test")
    _photo(app, "do-not-show")
    with repository.database.transaction() as connection:
        connection.execute("UPDATE photos SET never_display=1 WHERE id='do-not-show'")
    observed = []
    read_page = repository._candidate_page
    def bounded_page(*args):
        rows = read_page(*args)
        observed.append(len(rows))
        return rows
    monkeypatch.setattr(repository, "_candidate_page", bounded_page)
    monkeypatch.setattr(repository, "candidates", lambda *a, **k: pytest.fail("daily paths cannot materialize the window"))
    jobs = [service.ensure()["job_id"] for _ in range(3)]
    assert [service.jobs.get(job)["total_items"] for job in jobs] == [2, 2, 1]
    assert len({row["content_sha256"] for row in _tasks(service)}) == 5
    status = repository.status(date(2026, 10, 22), 5)
    assert status["candidate_count"] == 5
    assert observed and max(observed) <= 2
    assert not {"Screenshot-test", "do-not-show"} & {row["photo_id"] for row in _tasks(service)}


def test_next_day_waits_for_real_display_not_preparation(app):
    from datetime import datetime, timezone
    service = app.extensions["inktime_seasonal_analysis_service"]
    _photo(app, "morning")
    moment = datetime(2026, 10, 26, 0, 1, tzinfo=timezone.utc)
    assert service.preparation_target(moment) is None
    db = app.extensions["inktime_database"]
    with db.transaction() as connection:
        connection.execute("INSERT INTO display_history(photo_id,history_date,selection_method,displayed_at) "
                           "VALUES ('morning','2026-10-26','scheduled_display_prepare',?)", (moment.isoformat(),))
    assert service.preparation_target(moment) is None
    with db.transaction() as connection:
        connection.execute("INSERT INTO display_history(photo_id,history_date,selection_method,displayed_at,metadata_json) "
                           "VALUES ('morning','2026-10-26','device_queue_ack',?,?)",
                           (moment.isoformat(), json.dumps({"device_id": "frame-a"})))
    assert service.preparation_target(moment, device_id="frame-a") == date(2026, 10, 27)
    assert service.preparation_target(moment, device_id="frame-b") is None
    assert service.preparation_target(moment.replace(hour=23)) is None  # next local morning, no ACK


def test_weekday_and_weekend_day_playlists_use_target_date(app):
    schedules = app.extensions["inktime_offline_schedule_repository"]
    assert schedules.times_for_date(date(2026, 10, 30), ["08:00"]) == ["08:00", "18:00", "20:00", "22:00"]
    assert schedules.times_for_date(date(2026, 10, 31), ["08:00"]) == ["08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"]


def test_zero_photo_limits_mean_unlimited(app):
    photos = app.extensions["inktime_photo_repository"]
    assert photos.ai_limit_reached(daily_limit=0, monthly_limit=0) is False


def test_quality_lottery_does_not_count_prepared_photos_as_displayed():
    from inktime.app.domain.photos.rotation import exposure_count
    from inktime.app.domain.photos.seasonal_rotation import seasonal_order
    assert exposure_count({"selection_count": 50, "display_count": 0}) == 0
    rows = [{"id": "unseen-low", "selection_score": 35},
            {"id": "seen-high", "selection_score": 90, "display_count": 3},
            {"id": "unseen-high", "selection_score": 90}]
    for day in range(1, 29):
        ordered = seasonal_order(rows, date(2026, 10, day))
        assert ordered[-1]["id"] == "unseen-low"
        assert ordered == seasonal_order(rows, date(2026, 10, day))
    winners = {seasonal_order(rows, date(2026, 10, day))[0]["id"] for day in range(1, 29)}
    assert winners == {"seen-high", "unseen-high"}
