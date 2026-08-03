from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from inktime.app.workers.scheduler import SchedulerRunner


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
