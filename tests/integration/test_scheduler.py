from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
