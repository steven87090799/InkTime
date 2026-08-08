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


def test_scheduler_observability_is_deadline_gated(app):
    class FakeObservability:
        def __init__(self):
            self.heartbeats = []
            self.lightweight_ticks = 0
            self.platform_ticks = 0

        def heartbeat(self, source):
            self.heartbeats.append(source)

        def tick(self, *, include_platform, include_cleanup):
            assert include_platform is False
            assert include_cleanup is False
            self.lightweight_ticks += 1

        def platform_tick(self):
            self.platform_ticks += 1

    runner = SchedulerRunner(app)
    observability = FakeObservability()
    runner._run_observability(observability, 100.0)
    runner._run_observability(observability, 159.0)
    runner._run_observability(observability, 160.0)
    runner._run_observability(observability, 399.0)
    runner._run_observability(observability, 400.0)
    runner._run_observability(observability, 1000.0)

    assert observability.heartbeats == ["scheduler", "scheduler", "scheduler"]
    assert observability.lightweight_ticks == 4
    assert observability.platform_ticks == 2


def test_offline_shortage_pending_restart_does_not_claim_new_cooldown(app, monkeypatch):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線 pending restart 相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
        prefetch_lead_minutes=5,
    )
    scheduler = SchedulerRunner(app)
    repository = app.extensions["inktime_job_repository"]
    offline_schedules = app.extensions["inktime_offline_schedule_repository"]
    first_now = datetime(2026, 8, 3, 7, 55, tzinfo=timezone.utc)
    scheduler._prepare_due_offline_devices(first_now)
    first_job = next(
        job
        for job in repository.list()
        if '"offline_prepare"' in str(job["settings_json"]) and device_id in str(job["settings_json"])
    )
    worker = WorkerRunner(app)
    assert worker.run_once() == 1
    if repository.get(str(first_job["id"]))["status"] != "completed":
        assert worker.run_once() == 1

    with app.extensions["inktime_database"].session() as connection:
        config_version = int(
            connection.execute(
                "SELECT config_version FROM devices WHERE id=?", (device_id,)
            ).fetchone()[0]
        )
        retry_now = first_now + timedelta(hours=1)
        connection.execute(
            """
            UPDATE device_offline_schedules SET updated_at=?
            WHERE device_id=? AND target_date=? AND config_version=?
            """,
            (
                (retry_now - timedelta(seconds=SHORTAGE_RETRY_COOLDOWN_SECONDS + 1)).isoformat(),
                device_id,
                "2026-08-03",
                config_version,
            ),
        )
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            (retry_now.isoformat(), device_id),
        )

    job_service = app.extensions["inktime_job_service"]
    original_start = job_service.start
    failed_job_id = None

    def fail_new_job_once(job_id):
        nonlocal failed_job_id
        if failed_job_id is None:
            failed_job_id = str(job_id)
            raise JobFailure("temporary offline start failure", code="JOB-004")
        original_start(job_id)

    monkeypatch.setattr(job_service, "start", fail_new_job_once)
    scheduler._prepare_due_offline_devices(retry_now)
    assert failed_job_id is not None
    assert repository.get(failed_job_id)["status"] == "pending"
    with app.extensions["inktime_database"].session() as connection:
        deadline_after_start_failure = connection.execute(
            "SELECT next_offline_prepare_at FROM devices WHERE id=?", (device_id,)
        ).fetchone()["next_offline_prepare_at"]
    assert datetime.fromisoformat(deadline_after_start_failure) <= retry_now
    terminal_after_claim = offline_schedules.terminal_outcome_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=config_version,
    )
    assert terminal_after_claim is not None
    claimed_at = terminal_after_claim["updated_at"]

    scheduler._prepare_due_offline_devices(retry_now + timedelta(minutes=1))
    assert repository.get(failed_job_id)["status"] == "running"
    assert len(
        [
            job
            for job in repository.list()
            if '"offline_prepare"' in str(job["settings_json"]) and device_id in str(job["settings_json"])
        ]
    ) == 2
    terminal_after_restart = offline_schedules.terminal_outcome_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=config_version,
    )
    assert terminal_after_restart["updated_at"] == claimed_at


def test_offline_shortage_claim_crash_before_job_insert_is_restartable(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線 shortage claim crash 相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
        prefetch_lead_minutes=5,
    )
    scheduler = SchedulerRunner(app)
    repository = app.extensions["inktime_job_repository"]
    offline_schedules = app.extensions["inktime_offline_schedule_repository"]
    first_now = datetime(2026, 8, 3, 7, 55, tzinfo=timezone.utc)
    scheduler._prepare_due_offline_devices(first_now)
    assert WorkerRunner(app).run_once() == 1

    offline_jobs = [
        job
        for job in repository.list()
        if '"offline_prepare"' in str(job["settings_json"]) and device_id in str(job["settings_json"])
    ]
    assert len(offline_jobs) == 1
    first_job = offline_jobs[0]
    settings = json.loads(first_job["settings_json"])
    terminal_before_retry = offline_schedules.terminal_outcome_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
    )
    assert terminal_before_retry is not None
    retry_now = first_now + timedelta(hours=1)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE device_offline_schedules SET updated_at=? WHERE id=?",
            (
                (retry_now - timedelta(seconds=SHORTAGE_RETRY_COOLDOWN_SECONDS + 1)).isoformat(),
                terminal_before_retry["id"],
            ),
        )
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            (retry_now.isoformat(), device_id),
        )
    terminal_before_crash = offline_schedules.terminal_outcome_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
    )
    assert terminal_before_crash is not None
    claimed_updated_at = terminal_before_crash["updated_at"]

    def claim_then_crash(connection):
        assert offline_schedules.claim_terminal_outcome_retry(
            terminal_outcome=terminal_before_crash,
            device_id=device_id,
            target_date="2026-08-03",
            config_version=1,
            now=retry_now,
            connection=connection,
        )
        raise RuntimeError("simulated crash before offline Job insert")

    with pytest.raises(RuntimeError, match="simulated crash"):
        repository.create_maintenance_atomic(
            kind="render",
            name=str(first_job["name"]),
            priority=2,
            dedupe_key=str(first_job["dedupe_key"]),
            created_by=None,
            settings=settings,
            transaction_guard=claim_then_crash,
        )

    terminal_after_crash = offline_schedules.terminal_outcome_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
    )
    assert terminal_after_crash is not None
    assert terminal_after_crash["updated_at"] == claimed_updated_at

    restarted = SchedulerRunner(app)
    restarted._prepare_due_offline_devices(retry_now)
    offline_jobs = [
        job
        for job in repository.list()
        if '"offline_prepare"' in str(job["settings_json"]) and device_id in str(job["settings_json"])
    ]
    assert len(offline_jobs) == 2
    restarted._prepare_due_offline_devices(retry_now)
    assert len(
        [
            job
            for job in repository.list()
            if '"offline_prepare"' in str(job["settings_json"]) and device_id in str(job["settings_json"])
        ]
    ) == 2


def test_manual_schedule_run_inherits_retry_policy_without_cursor_ownership(app):
    task_key = "cache_cleanup"
    _due_task(app, task_key)
    schedules = app.extensions["inktime_schedule_repository"]
    database = app.extensions["inktime_database"]
    with database.session() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET timeout_seconds=321,retry_count=1,retry_interval_seconds=600 WHERE key=?",
            (task_key,),
        )
    task_before = schedules.get(task_key)
    assert task_before is not None
    next_run_before = task_before["next_run"]
    last_success_before = task_before["last_success"]

    SchedulerRunner(app)._enqueue_task(
        task_before,
        datetime.now(ZoneInfo("Asia/Taipei")),
        force=True,
        trigger_source="manual",
    )
    repository = app.extensions["inktime_job_repository"]
    manual_job = next(
        job
        for job in repository.list()
        if json.loads(job["settings_json"]).get("trigger_source") == "manual"
    )
    settings = json.loads(manual_job["settings_json"])
    assert settings["timeout_seconds"] == 321
    assert settings["max_retries"] == 1
    assert settings["max_attempts"] == 2
    assert settings["retry_interval_seconds"] == 600
    assert "scheduled_task" not in settings
    assert "scheduled_occurrence_at" not in settings
    assert manual_job["dedupe_key"] is None

    assert WorkerRunner(app).run_once() == 1
    task_after = schedules.get(task_key)
    assert task_after["next_run"] == next_run_before
    assert task_after["last_success"] == last_success_before


def test_scheduled_success_repairs_cursor_after_start_before_mark_enqueued(app):
    task_key = "cache_cleanup"
    dedupe_key = f"scheduled:{task_key}"
    _due_task(app, task_key)
    schedules = app.extensions["inktime_schedule_repository"]
    database = app.extensions["inktime_database"]
    repository = app.extensions["inktime_job_repository"]
    job_service = app.extensions["inktime_job_service"]

    old_next_run = (datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(minutes=1)).isoformat()
    with database.session() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET cron='0 * * * *',next_run=? WHERE key=?",
            (old_next_run, task_key),
        )
    task_before_start = schedules.get(task_key)
    assert task_before_start is not None
    old_next_run_dt = datetime.fromisoformat(str(task_before_start["next_run"]))

    job_id = repository.create_maintenance(
        kind="cleanup",
        name="排程成功游標恢復測試",
        settings={
            "scheduled_task": task_key,
            "scheduled_occurrence_at": task_before_start["next_run"],
            "trigger_source": "scheduler",
            "max_retries": 1,
            "max_attempts": 2,
            "retry_interval_seconds": 30,
        },
        created_by=None,
        dedupe_key=dedupe_key,
    )
    job_service.start(job_id)
    assert repository.get(job_id)["status"] == "running"
    assert schedules.get(task_key)["next_run"] == task_before_start["next_run"]

    # Simulate Scheduler crashing after start() and before mark_enqueued().
    assert WorkerRunner(app).run_once() == 1

    assert repository.get(job_id)["status"] == "completed"
    task_after_success = schedules.get(task_key)
    assert task_after_success is not None
    assert task_after_success["last_success"] is not None
    next_run = datetime.fromisoformat(str(task_after_success["next_run"]))
    assert next_run > old_next_run_dt

    SchedulerRunner(app).tick()
    assert len([job for job in repository.list() if job["dedupe_key"] == dedupe_key]) == 1


def test_scheduled_finalize_cursor_repair_is_atomic_with_job_terminalization(app, monkeypatch):
    task_key = "cache_cleanup"
    dedupe_key = f"scheduled:{task_key}"
    _due_task(app, task_key)
    schedules = app.extensions["inktime_schedule_repository"]
    database = app.extensions["inktime_database"]
    repository = app.extensions["inktime_job_repository"]
    job_service = app.extensions["inktime_job_service"]

    old_next_run = (datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(minutes=1)).isoformat()
    with database.session() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET cron='0 * * * *',next_run=? WHERE key=?",
            (old_next_run, task_key),
        )
    task_before_start = schedules.get(task_key)
    assert task_before_start is not None
    occurrence_at = str(task_before_start["next_run"])

    job_id = repository.create_maintenance(
        kind="cleanup",
        name="排程游標原子完成測試",
        settings={
            "scheduled_task": task_key,
            "scheduled_occurrence_at": occurrence_at,
            "trigger_source": "scheduler",
        },
        created_by=None,
        dedupe_key=dedupe_key,
    )
    job_service.start(job_id)
    original_record_success = schedules.record_success

    def fail_cursor_repair(*_args, **_kwargs):
        raise RuntimeError("injected scheduled cursor failure")

    monkeypatch.setattr(schedules, "record_success", fail_cursor_repair)
    with pytest.raises(RuntimeError, match="injected scheduled cursor failure"):
        WorkerRunner(app).run_once()

    assert repository.get(job_id)["status"] == "running"
    assert schedules.get(task_key)["next_run"] == occurrence_at

    monkeypatch.setattr(schedules, "record_success", original_record_success)
    assert WorkerRunner(app).run_once() == 1
    assert repository.get(job_id)["status"] == "completed"
    assert schedules.get(task_key)["last_success"] is not None

    SchedulerRunner(app).tick()
    assert len([job for job in repository.list() if job["dedupe_key"] == dedupe_key]) == 1


def test_worker_terminal_schedule_cursor_uses_general_timezone(app, monkeypatch):
    task_key = "cache_cleanup"
    schedules = app.extensions["inktime_schedule_repository"]
    database = app.extensions["inktime_database"]
    repository = app.extensions["inktime_job_repository"]
    job_service = app.extensions["inktime_job_service"]
    cache = app.extensions["inktime_thumbnail_cache"]

    occurrence_at = "2026-08-08T07:30:00+08:00"
    with database.session() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET cron='30 7 * * *',next_run=? WHERE key=?",
            (occurrence_at, task_key),
        )
    task_before_start = schedules.get(task_key)
    assert task_before_start is not None

    job_id = repository.create_maintenance(
        kind="cleanup",
        name="排程時區終止結果測試",
        settings={
            "scheduled_task": task_key,
            "scheduled_occurrence_at": occurrence_at,
            "trigger_source": "scheduler",
            "max_retries": 0,
            "max_attempts": 1,
            "retry_interval_seconds": 600,
        },
        created_by=None,
        dedupe_key="scheduled:cache_cleanup",
    )
    job_service.start(job_id)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr("inktime.app.workers.runner.datetime", FrozenDateTime)

    def fail_cleanup(*_args, **_kwargs):
        raise JobFailure("no content", code="NO_CONTENT")

    monkeypatch.setattr(cache, "cleanup", fail_cleanup)
    assert WorkerRunner(app).run_once() == 1

    task_after = schedules.get(task_key)
    assert task_after is not None
    assert task_after["next_run"] == "2026-08-09T07:30:00+08:00"


def test_scheduled_success_preserves_cursor_after_mark_enqueued(app):
    task_key = "cache_cleanup"
    _due_task(app, task_key)
    schedules = app.extensions["inktime_schedule_repository"]
    database = app.extensions["inktime_database"]

    old_next_run = (datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(minutes=1)).isoformat()
    with database.session() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET cron='0 * * * *',next_run=? WHERE key=?",
            (old_next_run, task_key),
        )
    task_before_enqueue = schedules.get(task_key)
    assert task_before_enqueue is not None
    occurrence_at = str(task_before_enqueue["next_run"])

    schedules.mark_enqueued(task_before_enqueue, datetime.now(ZoneInfo("Asia/Taipei")))
    task_after_enqueue = schedules.get(task_key)
    assert task_after_enqueue is not None
    marked_next_run = str(task_after_enqueue["next_run"])

    # Simulate a slow Worker completion after the next normal occurrence.
    completion_now = datetime.fromisoformat(marked_next_run) + timedelta(minutes=30)
    schedules.record_success(
        task_after_enqueue,
        completion_now,
        scheduled_occurrence_at=occurrence_at,
    )

    completed_task = schedules.get(task_key)
    assert completed_task is not None
    assert completed_task["last_success"] is not None
    assert completed_task["next_run"] == marked_next_run


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

    offline_schedules = app.extensions["inktime_offline_schedule_repository"]
    state = offline_schedules.transient_recovery_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
    )
    assert state is not None
    assert state["failure_count"] == 1
    assert state["backoff_seconds"] == 1800

    scheduler._prepare_due_offline_devices(now + timedelta(minutes=6))
    assert len(offline_jobs()) == 1

    with app.extensions["inktime_database"].session() as connection:
        snapshot_row = connection.execute(
            "SELECT snapshot_json FROM device_offline_schedules WHERE device_id=? AND target_date=? AND config_version=1",
            (device_id, "2026-08-03"),
        ).fetchone()
        snapshot = json.loads(snapshot_row["snapshot_json"])
        snapshot["transient_recovery"]["next_retry_at"] = (
            now + timedelta(minutes=30)
        ).isoformat()
        connection.execute(
            "UPDATE device_offline_schedules SET snapshot_json=? WHERE device_id=? AND target_date=? AND config_version=1",
            (json.dumps(snapshot), device_id, "2026-08-03"),
        )
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            ((now + timedelta(minutes=30)).isoformat(), device_id),
        )
    scheduler._prepare_due_offline_devices(now + timedelta(minutes=29))
    assert len(offline_jobs()) == 1
    scheduler._prepare_due_offline_devices(now + timedelta(minutes=30, seconds=1))
    assert len(offline_jobs()) == 2


def test_offline_transient_cross_job_backoff_is_durable_bounded_and_reset(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線跨工作 cooldown 相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
        prefetch_lead_minutes=5,
    )
    repository = app.extensions["inktime_offline_schedule_repository"]
    base = datetime(2026, 8, 3, 7, 55, tzinfo=timezone.utc)
    expected = [1800, 3600, 7200, 14400, 14400]
    for failure_count, backoff_seconds in enumerate(expected, start=1):
        state = repository.record_transient_exhausted(
            device_id=device_id,
            target_date="2026-08-03",
            config_version=1,
            now=base,
        )
        assert state["failure_count"] == failure_count
        assert state["backoff_seconds"] == backoff_seconds
        assert state["next_retry_at"] == (
            base + timedelta(seconds=backoff_seconds)
        ).isoformat()
    assert repository.transient_recovery_blocks_retry(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
        now=base + timedelta(seconds=14399),
    )
    assert not repository.transient_recovery_blocks_retry(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
        now=base + timedelta(seconds=14400),
    )
    repository.clear_transient_recovery(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
    )
    assert repository.transient_recovery_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
    ) is None


def test_offline_transient_finalize_crash_preserves_backoff(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線 transient finalize crash 相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
        prefetch_lead_minutes=5,
    )
    database = app.extensions["inktime_database"]
    repository = app.extensions["inktime_job_repository"]
    offline_schedules = app.extensions["inktime_offline_schedule_repository"]
    job_id = repository.create_maintenance(
        kind="render",
        name="離線 transient finalize crash 測試",
        settings={
            "offline_prepare": {
                "device_id": device_id,
                "target_date": "2026-08-03",
                "config_version": 1,
            },
            "trigger_source": "offline-scheduler",
        },
        created_by=None,
        dedupe_key=f"offline-prepare:{device_id}:2026-08-03:1",
    )
    app.extensions["inktime_job_service"].start(job_id)
    with database.session() as connection:
        connection.execute(
            "UPDATE job_items SET status='failed',error_code='DISPLAY-005' WHERE job_id=?",
            (job_id,),
        )

    exhausted_at = datetime(2026, 8, 3, 7, 55, tzinfo=timezone.utc)

    def persist_then_crash(connection, finalized_job_id, target):
        assert finalized_job_id == job_id
        assert target == "completed_with_errors"
        offline_schedules.record_transient_exhausted(
            device_id=device_id,
            target_date="2026-08-03",
            config_version=1,
            now=exhausted_at,
            connection=connection,
        )
        raise RuntimeError("simulated transient finalization crash")

    with pytest.raises(RuntimeError, match="simulated transient"):
        repository.finalize_if_done(job_id, finalizer=persist_then_crash)

    assert repository.get(job_id)["status"] == "running"
    assert offline_schedules.transient_recovery_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
    ) is None

    def persist_recovery(connection, _finalized_job_id, _target):
        offline_schedules.record_transient_exhausted(
            device_id=device_id,
            target_date="2026-08-03",
            config_version=1,
            now=exhausted_at,
            connection=connection,
        )

    assert repository.finalize_if_done(job_id, finalizer=persist_recovery)
    assert repository.get(job_id)["status"] == "completed_with_errors"
    state = offline_schedules.transient_recovery_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
    )
    assert state is not None
    assert state["backoff_seconds"] == 1800

    SchedulerRunner(app)._prepare_due_offline_devices(exhausted_at + timedelta(seconds=1))
    assert len(
        [
            job
            for job in repository.list()
            if '"offline_prepare"' in str(job["settings_json"]) and device_id in str(job["settings_json"])
        ]
    ) == 1


def test_stale_offline_failure_cannot_demote_concurrent_ready_playlist(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線 ready guard 相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    database = app.extensions["inktime_database"]
    repository = app.extensions["inktime_offline_schedule_repository"]
    exhausted_at = datetime(2026, 8, 3, 7, 55, tzinfo=timezone.utc)
    repository.record_transient_exhausted(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
        now=exhausted_at,
    )
    preserved_snapshot = {
        "playlist_version": "playlist-kept",
        "slots": ["slot-kept"],
    }
    with database.session() as connection:
        before = connection.execute(
            "SELECT id,updated_at FROM device_offline_schedules WHERE device_id=? AND target_date=? AND config_version=1",
            (device_id, "2026-08-03"),
        ).fetchone()
        assert before is not None
        connection.execute(
            "UPDATE device_offline_schedules SET status='ready',terminal_outcome_code=NULL,snapshot_json=? WHERE id=?",
            (json.dumps(preserved_snapshot, sort_keys=True), before["id"]),
        )

    assert repository.record_transient_exhausted(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=1,
        now=exhausted_at + timedelta(seconds=1),
    ) == {}
    with database.session() as connection:
        after = connection.execute(
            "SELECT status,updated_at,snapshot_json,terminal_outcome_code FROM device_offline_schedules WHERE device_id=? AND target_date=? AND config_version=1",
            (device_id, "2026-08-03"),
        ).fetchone()
    assert after["status"] == "ready"
    assert after["updated_at"] == before["updated_at"]
    assert json.loads(after["snapshot_json"]) == preserved_snapshot
    assert after["terminal_outcome_code"] is None


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

    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            (now.isoformat(), device_id),
        )
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
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            (retry_now.isoformat(), device_id),
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


def test_offline_prefetch_deadline_query_processes_a_bounded_batch_without_cursor(app, monkeypatch):
    repository = app.extensions["inktime_offline_schedule_repository"]
    monkeypatch.setattr(
        repository,
        "advance_prefetch_cursor",
        lambda *_args, **_kwargs: pytest.fail("runtime must not advance the legacy prefetch cursor"),
    )
    monkeypatch.setattr(
        repository,
        "reset_prefetch_cursor",
        lambda *_args, **_kwargs: pytest.fail("runtime must not reset the legacy prefetch cursor"),
    )
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
    for _index in range(3):
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


def test_offline_prefetch_active_due_page_advances_and_reaches_tail(app):
    device_ids = []
    for index in range(11):
        device_id, _token = app.extensions["inktime_device_repository"].create(
            f"活動工作頁尾裝置 {index:02d}",
            delivery_mode="inktime_offline_schedule",
            offline_prefetch_allowed=True,
            schedule_times=["08:00"],
            prefetch_lead_minutes=5,
        )
        device_ids.append(device_id)

    runner = SchedulerRunner(app)
    repository = app.extensions["inktime_job_repository"]
    now = datetime(2026, 8, 3, 7, 55, tzinfo=ZoneInfo("Asia/Taipei"))
    runner._prepare_due_offline_devices(now)

    def offline_jobs(device_id):
        return [
            job
            for job in repository.list()
            if '"offline_prepare"' in str(job["settings_json"])
            and device_id in str(job["settings_json"])
        ]

    assert all(len(offline_jobs(device_id)) == 1 for device_id in device_ids[:10])
    assert offline_jobs(device_ids[-1]) == []

    # Simulate the first page remaining due while its Jobs are still active.
    # The scheduler must advance those committed targets before the next page
    # can be selected; it must not enqueue duplicates for them.
    with app.extensions["inktime_database"].session() as connection:
        connection.executemany(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            [(now.isoformat(), device_id) for device_id in device_ids[:10]],
        )

    runner._prepare_due_offline_devices(now)
    assert all(len(offline_jobs(device_id)) == 1 for device_id in device_ids[:10])
    assert offline_jobs(device_ids[-1]) == []

    runner._prepare_due_offline_devices(now)
    assert len(offline_jobs(device_ids[-1])) == 1
    assert all(len(offline_jobs(device_id)) == 1 for device_id in device_ids)


def test_offline_prefetch_quarantines_ambiguous_capability_rows(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "能力未確認的離線裝置",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE devices SET offline_schedule_capability_state='legacy_ambiguous' WHERE id=?",
            (device_id,),
        )

    SchedulerRunner(app)._prepare_due_offline_devices(
        datetime(2026, 8, 3, 7, 55, tzinfo=ZoneInfo("Asia/Taipei"))
    )

    assert not [
        job
        for job in app.extensions["inktime_job_repository"].list()
        if '"offline_prepare"' in str(job["settings_json"]) and device_id in str(job["settings_json"])
    ]


def test_offline_prefetch_does_not_write_when_deadline_is_in_the_future(app, monkeypatch):
    repository = app.extensions["inktime_offline_schedule_repository"]
    monkeypatch.setattr(
        repository,
        "advance_prefetch_cursor",
        lambda *_args, **_kwargs: pytest.fail("runtime must not advance the legacy prefetch cursor"),
    )
    monkeypatch.setattr(
        repository,
        "reset_prefetch_cursor",
        lambda *_args, **_kwargs: pytest.fail("runtime must not reset the legacy prefetch cursor"),
    )
    device_ids = []
    for index in range(3):
        device_id, _token = app.extensions["inktime_device_repository"].create(
            f"未到期離線裝置 {index:02d}",
            delivery_mode="inktime_offline_schedule",
            offline_prefetch_allowed=True,
            schedule_times=["08:00"],
            prefetch_lead_minutes=5,
        )
        device_ids.append(device_id)
    with app.extensions["inktime_database"].session() as connection:
        connection.executemany(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            [("2026-08-04T00:00:00+00:00", device_id) for device_id in device_ids],
        )

    runner = SchedulerRunner(app)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    for _index in range(3):
        runner._prepare_due_offline_devices(now)

    assert not [
        job
        for job in app.extensions["inktime_job_repository"].list()
        if '"offline_prepare"' in str(job["settings_json"])
    ]
    with app.extensions["inktime_database"].session() as connection:
        cursor = connection.execute(
            "SELECT last_device_id FROM device_offline_prefetch_cursors WHERE id=1"
        ).fetchone()
    assert cursor["last_device_id"] is None


def test_offline_prefetch_deadline_query_orders_due_devices_and_reports_bounded_tail(app):
    repository = app.extensions["inktime_offline_schedule_repository"]
    device_ids = []
    for index in range(11):
        device_id, _token = app.extensions["inktime_device_repository"].create(
            f"尾頁輪轉裝置 {index:02d}",
            delivery_mode="inktime_offline_schedule",
            offline_prefetch_allowed=True,
            schedule_times=["20:00"],
            prefetch_lead_minutes=5,
        )
        device_ids.append(device_id)
    with app.extensions["inktime_database"].session() as connection:
        connection.executemany(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            [("2026-08-03T00:00:00+00:00", device_id) for device_id in device_ids[:10]]
            + [("2026-08-04T00:00:00+00:00", device_ids[-1])],
        )

    batch, has_more = repository.due_prefetch_devices(
        limit=10,
        now=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
    )
    assert [str(device["id"]) for device in batch] == sorted(device_ids)[:10]
    assert has_more is False
