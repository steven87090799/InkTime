from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from inktime.app.domain.jobs.failure_policy import JobFailure
from inktime.app.repositories.offline_schedules import SHORTAGE_RETRY_COOLDOWN_SECONDS
from inktime.app.workers.scheduler import (
    OFFLINE_PREPARE_RETRY_INTERVAL_SECONDS,
    SchedulerRunner,
)
from inktime.app.workers.runner import WorkerRunner


def _due_task(app, key: str, **config):
    schedules = app.extensions["inktime_schedule_repository"]
    task = schedules.get(key)
    assert task is not None
    current_config = task["config"] | config
    schedules.update(
        key,
        {
            "enabled": True,
            "cron": "* * * * *",
            "weekdays": [],
            "start_time": "00:00",
            "window_start": None,
            "window_end": None,
            "timeout_seconds": 300,
            "retry_count": 1,
            "retry_interval_seconds": 30,
            "config": current_config,
        },
        "Asia/Taipei",
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET next_run=? WHERE key=?",
            ((datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(minutes=1)).isoformat(), key),
        )


def test_due_incremental_schedule_enqueues_existing_scanner_entry(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _due_task(app, "incremental_scan", root_path=str(root), delay_high_load=False)
    SchedulerRunner(app).tick()
    jobs = app.extensions["inktime_job_repository"].list()
    job = next(job for job in jobs if job["settings_json"].find('"scheduled_task": "incremental_scan"') >= 0)
    assert job["kind"] == "scan"
    assert job["status"] == "running"
    assert json.loads(job["settings_json"])["max_attempts"] == 2


def test_due_scheduled_pending_job_restarts_without_duplicate(app, monkeypatch):
    task_key = "cache_cleanup"
    dedupe_key = f"scheduled:{task_key}"
    _due_task(app, task_key)
    schedules = app.extensions["inktime_schedule_repository"]
    database = app.extensions["inktime_database"]
    repository = app.extensions["inktime_job_repository"]
    job_service = app.extensions["inktime_job_service"]
    job_id = repository.create_maintenance(
        kind="cleanup",
        name="排程待處理恢復測試",
        settings={
            "scheduled_task": task_key,
            "trigger_source": "scheduler",
            "max_retries": 1,
            "max_attempts": 2,
            "retry_interval_seconds": 30,
        },
        created_by=None,
        dedupe_key=dedupe_key,
    )

    original_start = job_service.start
    start_calls = 0

    def fail_once_then_start(existing_job_id):
        nonlocal start_calls
        start_calls += 1
        if start_calls == 1:
            raise JobFailure("temporary scheduler start failure", code="JOB-004")
        original_start(existing_job_id)

    monkeypatch.setattr(job_service, "start", fail_once_then_start)
    runner = SchedulerRunner(app)

    runner.tick()
    assert repository.get(job_id)["status"] == "pending"
    assert len([job for job in repository.list() if job["dedupe_key"] == dedupe_key]) == 1
    failed_task = schedules.get(task_key)
    assert failed_task["error_status"] == "temporary scheduler start failure"
    assert datetime.fromisoformat(str(failed_task["next_run"])) > datetime.now(
        ZoneInfo("Asia/Taipei")
    )

    with database.session() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET next_run=? WHERE key=?",
            ((datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(minutes=1)).isoformat(), task_key),
        )
    runner.tick()
    assert repository.get(job_id)["status"] == "running"
    assert schedules.get(task_key)["error_status"] is None
    assert len([job for job in repository.list() if job["dedupe_key"] == dedupe_key]) == 1

    with database.session() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET next_run=? WHERE key=?",
            ((datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(minutes=1)).isoformat(), task_key),
        )
    runner.tick()
    assert repository.get(job_id)["status"] == "running"
    assert len([job for job in repository.list() if job["dedupe_key"] == dedupe_key]) == 1


def test_one_scheduled_task_failure_does_not_stop_the_next_task(app, monkeypatch):
    _due_task(app, "incremental_scan", delay_high_load=False)
    _due_task(app, "cache_cleanup")
    seen = []
    original = SchedulerRunner._enqueue_task

    def enqueue(self, task, now, *, force=False):
        seen.append(task["key"])
        if task["key"] == "incremental_scan":
            raise RuntimeError("預期失敗")
        return original(self, task, now, force=force)

    monkeypatch.setattr(SchedulerRunner, "_enqueue_task", enqueue)
    SchedulerRunner(app).tick()
    assert "incremental_scan" in seen
    assert "cache_cleanup" in seen
    assert app.extensions["inktime_schedule_repository"].get("incremental_scan")["error_status"] == "預期失敗"


def test_scheduled_retry_exhaustion_returns_to_normal_cron_without_replacement(app, monkeypatch):
    schedules = app.extensions["inktime_schedule_repository"]
    database = app.extensions["inktime_database"]
    with database.session() as connection:
        connection.execute(
            """UPDATE scheduled_tasks
               SET cron='0 0 1 1 *',retry_count=1,retry_interval_seconds=600
               WHERE key='display_prepare'"""
        )

    repository = app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(
        kind="render",
        name="排程重試耗盡測試",
        settings={
            "scheduled_task": "display_prepare",
            "trigger_source": "scheduler",
            "max_retries": 1,
            "max_attempts": 2,
            "retry_interval_seconds": 600,
            "display_prepare": {},
        },
        created_by=None,
        dedupe_key="scheduled:display_prepare",
    )
    app.extensions["inktime_job_service"].start(job_id)

    def fail_prepare(*_args, **_kwargs):
        raise RuntimeError("temporary outage")

    monkeypatch.setattr(
        app.extensions["inktime_display_preparation_service"], "prepare", fail_prepare
    )
    runner = WorkerRunner(app)
    assert runner.run_once() == 1
    with database.session() as connection:
        connection.execute(
            "UPDATE job_items SET available_at=? WHERE job_id=?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
    assert runner.run_once() == 1

    job = repository.get(job_id)
    assert job["status"] == "completed_with_errors"
    assert len(repository.list()) == 1
    task = schedules.get("display_prepare")
    assert task is not None
    assert task["error_status"].startswith("JOB-003")
    next_run = datetime.fromisoformat(str(task["next_run"]))
    assert next_run > datetime.now(next_run.tzinfo) + timedelta(seconds=600)


def test_offline_prefetch_creates_one_deduplicated_render_job(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "排程離線相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
        prefetch_lead_minutes=5,
    )
    runner = SchedulerRunner(app)
    now = datetime(2026, 8, 3, 7, 55, tzinfo=timezone.utc)
    runner._prepare_due_offline_devices(now)
    runner._prepare_due_offline_devices(now)

    jobs = app.extensions["inktime_job_repository"].list()
    offline_jobs = [
        job
        for job in jobs
        if '"offline_prepare"' in str(job["settings_json"])
        and device_id in str(job["settings_json"])
    ]
    assert len(offline_jobs) == 1
    assert offline_jobs[0]["dedupe_key"] == (
        f"offline-prepare:{device_id}:2026-08-03:1"
    )
    settings = json.loads(offline_jobs[0]["settings_json"])
    assert settings["max_retries"] == 1
    assert settings["max_attempts"] == 2
    assert settings["retry_interval_seconds"] == OFFLINE_PREPARE_RETRY_INTERVAL_SECONDS


def test_offline_prepare_transient_failure_keeps_active_retry_bounded(app, monkeypatch):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線 transient retry 相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
        prefetch_lead_minutes=5,
    )
    scheduler = SchedulerRunner(app)
    now = datetime(2026, 8, 3, 7, 55, tzinfo=timezone.utc)
    scheduler._prepare_due_offline_devices(now)
    repository = app.extensions["inktime_job_repository"]

    def offline_jobs():
        return [
            job
            for job in repository.list()
            if '"offline_prepare"' in str(job["settings_json"])
            and device_id in str(job["settings_json"])
        ]

    jobs = offline_jobs()
    assert len(jobs) == 1
    job_id = str(jobs[0]["id"])
    settings = json.loads(jobs[0]["settings_json"])
    assert settings["max_attempts"] == 2
    assert settings["retry_interval_seconds"] == 600

    def fail_prepare(**_kwargs):
        raise JobFailure("temporary display outage", code="DISPLAY-005")

    monkeypatch.setattr(
        app.extensions["inktime_display_preparation_service"],
        "prepare_device_day",
        fail_prepare,
    )
    worker = WorkerRunner(app)
    assert worker.run_once() == 1

    item = repository.list_items(job_id)[0]
    assert item["status"] == "pending"
    available_at = datetime.fromisoformat(str(item["available_at"]))
    assert available_at - datetime.now(timezone.utc) > timedelta(seconds=590)
    assert repository.get(job_id)["status"] in {"running", "retrying"}

    scheduler._prepare_due_offline_devices(now + timedelta(minutes=1))
    scheduler._prepare_due_offline_devices(now + timedelta(minutes=5))
    assert len(offline_jobs()) == 1

    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE job_items SET available_at=? WHERE job_id=?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
    assert worker.run_once() == 1
    assert repository.get(job_id)["status"] == "completed_with_errors"

    scheduler._prepare_due_offline_devices(now + timedelta(minutes=6))
    assert len(offline_jobs()) == 2


def test_offline_shortage_is_terminal_for_one_device_day_config(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線內容不足相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
        prefetch_lead_minutes=5,
    )
    scheduler = SchedulerRunner(app)
    now = datetime(2026, 8, 3, 7, 55, tzinfo=timezone.utc)
    scheduler._prepare_due_offline_devices(now)
    repository = app.extensions["inktime_job_repository"]
    offline_jobs = [
        job
        for job in repository.list()
        if '"offline_prepare"' in str(job["settings_json"])
        and device_id in str(job["settings_json"])
    ]
    assert len(offline_jobs) == 1

    assert WorkerRunner(app).run_once() == 1
    completed = repository.get(offline_jobs[0]["id"])
    assert completed["status"] == "completed"
    with app.extensions["inktime_database"].session() as connection:
        result = connection.execute(
            "SELECT result_json,error_code FROM job_items WHERE job_id=?",
            (offline_jobs[0]["id"],),
        ).fetchone()
    assert json.loads(result["result_json"])["outcome_code"] == "NO_ELIGIBLE_CANDIDATES"
    assert result["error_code"] is None

    scheduler._prepare_due_offline_devices(now)
    scheduler._prepare_due_offline_devices(now + timedelta(minutes=1))
    assert len(
        [
            job
            for job in repository.list()
            if '"offline_prepare"' in str(job["settings_json"])
            and device_id in str(job["settings_json"])
        ]
    ) == 1
    terminal = app.extensions["inktime_offline_schedule_repository"].terminal_outcome_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
    )
    assert terminal["terminal_outcome_code"] == "NO_ELIGIBLE_CANDIDATES"


def test_offline_shortage_retries_after_bounded_cooldown_and_keeps_active_dedupe(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "冷卻後恢復的離線相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
        prefetch_lead_minutes=5,
    )
    scheduler = SchedulerRunner(app)
    first_now = datetime(2026, 8, 3, 7, 55, tzinfo=timezone.utc)
    scheduler._prepare_due_offline_devices(first_now)
    repository = app.extensions["inktime_job_repository"]
    first_jobs = [
        job
        for job in repository.list()
        if '"offline_prepare"' in str(job["settings_json"])
        and device_id in str(job["settings_json"])
    ]
    assert len(first_jobs) == 1
    first_job_id = first_jobs[0]["id"]
    assert WorkerRunner(app).run_once() == 1

    with app.extensions["inktime_database"].session() as connection:
        config_version = int(
            connection.execute(
                "SELECT config_version FROM devices WHERE id=?", (device_id,)
            ).fetchone()[0]
        )

    retry_now = first_now + timedelta(hours=1)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE device_offline_schedules
            SET updated_at=?
            WHERE device_id=? AND target_date=? AND config_version=?
            """,
            (
                (
                    retry_now - timedelta(seconds=SHORTAGE_RETRY_COOLDOWN_SECONDS + 1)
                ).isoformat(),
                device_id,
                "2026-08-03",
                config_version,
            ),
        )

    scheduler._prepare_due_offline_devices(retry_now)
    offline_jobs = [
        job
        for job in repository.list()
        if '"offline_prepare"' in str(job["settings_json"])
        and device_id in str(job["settings_json"])
    ]
    assert len(offline_jobs) == 2
    retry_jobs = [job for job in offline_jobs if job["id"] != first_job_id]
    assert len(retry_jobs) == 1
    retry_job = retry_jobs[0]
    assert retry_job["status"] == "running"
    terminal = app.extensions["inktime_offline_schedule_repository"].terminal_outcome_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=config_version,
    )
    assert terminal["updated_at"] == retry_now.isoformat()

    scheduler._prepare_due_offline_devices(retry_now)
    assert len(
        [
            job
            for job in repository.list()
            if '"offline_prepare"' in str(job["settings_json"])
            and device_id in str(job["settings_json"])
        ]
    ) == 2
    assert repository.transition(
        retry_job["id"], {"running"}, "failed", "test_transient_failure"
    )
    scheduler._prepare_due_offline_devices(retry_now + timedelta(minutes=1))
    scheduler._prepare_due_offline_devices(retry_now + timedelta(minutes=5))
    assert len(
        [
            job
            for job in repository.list()
            if '"offline_prepare"' in str(job["settings_json"])
            and device_id in str(job["settings_json"])
        ]
    ) == 2

    scheduler._prepare_due_offline_devices(
        retry_now + timedelta(seconds=SHORTAGE_RETRY_COOLDOWN_SECONDS + 1)
    )
    assert len(
        [
            job
            for job in repository.list()
            if '"offline_prepare"' in str(job["settings_json"])
            and device_id in str(job["settings_json"])
        ]
    ) == 3


def test_offline_scheduler_prepares_only_tomorrow_after_expired_today(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "預先準備明日的離線相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
        prefetch_lead_minutes=5,
    )
    runner = SchedulerRunner(app)
    # 12:00 UTC is 20:00 in the device's Asia/Taipei zone.
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    runner._prepare_due_offline_devices(now)
    runner._prepare_due_offline_devices(now)

    jobs = app.extensions["inktime_job_repository"].list()
    offline_jobs = [
        job
        for job in jobs
        if '"offline_prepare"' in str(job["settings_json"])
        and device_id in str(job["settings_json"])
    ]
    assert {job["dedupe_key"] for job in offline_jobs} == {
        f"offline-prepare:{device_id}:2026-08-04:1",
    }
    assert len(offline_jobs) == 1


def test_server_prepare_margin_stays_before_device_prefetch(app):
    zone = ZoneInfo("Asia/Taipei")
    schedule = ["08:00"]
    assert SchedulerRunner._offline_prefetch_target_date(
        datetime(2026, 8, 3, 7, 39, tzinfo=zone), schedule, 5, 15
    ) is None
    assert SchedulerRunner._offline_prefetch_target_date(
        datetime(2026, 8, 3, 7, 40, tzinfo=zone), schedule, 5, 15
    ).isoformat() == "2026-08-03"


def test_offline_prefetch_target_date_rejects_invalid_time_contracts_and_catches_up():
    zone = ZoneInfo("Asia/Taipei")
    now = datetime(2026, 8, 9, 9, 0, tzinfo=zone)
    with pytest.raises(ValueError, match="prefetch_lead_minutes"):
        SchedulerRunner._offline_prefetch_target_date(now, ["08:00"], -1)
    with pytest.raises(ValueError, match="server_prefetch_margin_minutes"):
        SchedulerRunner._offline_prefetch_target_date(now, ["08:00"], 5, 61)
    with pytest.raises(ValueError, match="時區"):
        SchedulerRunner._offline_prefetch_target_date(datetime(2026, 8, 7, 9, 0), ["08:00"], 5)
    assert SchedulerRunner._offline_prefetch_target_date(now, ["08:00"], 0) is None


def test_offline_prefetch_keeps_today_and_tomorrow_decisions_independent():
    zone = ZoneInfo("Asia/Taipei")
    evening = datetime(2026, 8, 3, 20, 0, tzinfo=zone)
    assert SchedulerRunner._offline_prefetch_target_dates(
        evening, ["08:00"], 5, 15, 20
    ) == [datetime(2026, 8, 4, tzinfo=zone).date()]
    assert SchedulerRunner._offline_prefetch_target_dates(
        evening, ["08:00", "22:00"], 5, 15, 20
    ) == [
        datetime(2026, 8, 3, tzinfo=zone).date(),
        datetime(2026, 8, 4, tzinfo=zone).date(),
    ]
    late_today = datetime(2026, 8, 3, 22, 0, tzinfo=zone)
    assert SchedulerRunner._offline_prefetch_target_dates(
        late_today, ["00:10", "23:00"], 120, 60, 20
    ) == [
        datetime(2026, 8, 3, tzinfo=zone).date(),
        datetime(2026, 8, 4, tzinfo=zone).date(),
    ]
    technical_deadline = datetime(2026, 8, 3, 21, 10, tzinfo=zone)
    assert SchedulerRunner._offline_prefetch_target_dates(
        technical_deadline, ["00:10"], 120, 60, 23
    ) == [datetime(2026, 8, 4, tzinfo=zone).date()]


def test_offline_scheduler_skips_expired_today_but_keeps_a_future_today_slot(app):
    def offline_jobs(device_id):
        return {
            job["dedupe_key"]
            for job in app.extensions["inktime_job_repository"].list()
            if '"offline_prepare"' in str(job["settings_json"])
            and device_id in str(job["settings_json"])
        }

    expired_id, _token = app.extensions["inktime_device_repository"].create(
        "只有早上時刻",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    future_id, _token = app.extensions["inktime_device_repository"].create(
        "晚上仍有時刻",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "22:00"],
    )
    runner = SchedulerRunner(app)
    now = datetime(2026, 8, 2, 23, 0, tzinfo=timezone.utc)
    runner._prepare_due_offline_devices(now)
    assert offline_jobs(expired_id) == set()
    assert offline_jobs(future_id) == set()

    now = datetime(2026, 8, 2, 23, 30, tzinfo=timezone.utc)
    runner._prepare_due_offline_devices(now)
    assert offline_jobs(expired_id) == set()
    assert offline_jobs(future_id) == set()

    # 20:00 Asia/Taipei: only the device with a future 22:00 Slot keeps a
    # meaningful today target; both devices get tomorrow.
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    runner._prepare_due_offline_devices(now)
    assert offline_jobs(expired_id) == {
        f"offline-prepare:{expired_id}:2026-08-04:1"
    }
    assert offline_jobs(future_id) == {
        f"offline-prepare:{future_id}:2026-08-03:1",
        f"offline-prepare:{future_id}:2026-08-04:1",
    }


def test_offline_prefetch_cursor_eventually_visits_more_than_first_ten_devices(app):
    device_ids = []
    for index in range(25):
        device_id, _token = app.extensions["inktime_device_repository"].create(
            f"大量離線裝置 {index:02d}",
            delivery_mode="inktime_offline_schedule",
            offline_prefetch_allowed=True,
            schedule_times=["08:00"],
            prefetch_lead_minutes=5,
        )
        device_ids.append(device_id)
    runner = SchedulerRunner(app)
    now = datetime(2026, 8, 3, 7, 55, tzinfo=ZoneInfo("Asia/Taipei"))
    for _index in range(4):
        runner._prepare_due_offline_devices(now)

    jobs = app.extensions["inktime_job_repository"].list()
    prepared_ids = {
        str(device_id)
        for job in jobs
        if '"offline_prepare"' in str(job["settings_json"])
        for device_id in device_ids
        if str(device_id) in str(job["settings_json"])
    }
    assert prepared_ids == set(device_ids)
