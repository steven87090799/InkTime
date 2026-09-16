from __future__ import annotations

import threading
import time
import os
from pathlib import Path

from inktime.app.workers.job_worker import BoundedJobWorker


def _blocked_file_read(*, path, marker):
    Path(marker).write_text("started")
    with open(path, "rb") as stream:
        return stream.read(1)


def test_runner_reaps_blocked_local_io_and_continues_next_job(app, tmp_path, monkeypatch):
    from inktime.app.workers.runner import WorkerRunner, _run_local_job_item

    fifo = tmp_path / "blocked-io"
    marker = tmp_path / "started"
    os.mkfifo(fifo)
    repository = app.extensions["inktime_job_repository"]
    jobs = []
    for kind in ("cleanup", "backup"):
        job_id = repository.create_maintenance(
            kind=kind, name=kind, settings={"timeout_seconds": 6}, created_by="test",
        )
        app.extensions["inktime_job_service"].start(job_id)
        jobs.append(job_id)
    boundary = app.extensions["inktime_process_boundary"]
    original_call = boundary.call

    def call(function, **kwargs):
        assert function is _run_local_job_item
        if kwargs["kwargs"]["context"]["job"]["kind"] == "cleanup":
            return original_call(_blocked_file_read, timeout_seconds=5,
                                 kwargs={"path": str(fifo), "marker": str(marker)})
        return {"backup": "finished", "removed": 0}

    monkeypatch.setattr(boundary, "call", call)
    started = time.monotonic()
    WorkerRunner(app, isolate_local=True).run_once()
    assert time.monotonic() - started < 15
    assert marker.is_file()
    assert boundary.observability()["active"] == 0
    assert repository.get(jobs[1])["status"] == "completed"
    with app.extensions["inktime_database"].session() as connection:
        item = connection.execute("SELECT attempts,error_code,lease_until FROM job_items WHERE job_id=?", (jobs[0],)).fetchone()
        assert item["attempts"] == 1
        assert item["error_code"] == "JOB-LOCAL-TIMEOUT"
        assert item["lease_until"] is None


def test_timed_out_future_is_tracked_to_completion_without_retry_or_double_charge(app):
    repository = app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(
        kind="cleanup",
        name="timeout",
        settings={},
        created_by="test",
    )
    app.extensions["inktime_job_service"].start(job_id)
    calls = 0
    calls_lock = threading.Lock()

    def slow_processor(_item):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(1.2)
        return {"stage": "late", "_actual_cost": 0.25}

    worker = BoundedJobWorker(
        repository,
        slow_processor,
        concurrency=1,
        queue_multiplier=1,
        timeout_seconds=1,
    )
    worker.run_job(job_id)

    with app.extensions["inktime_database"].session() as connection:
        item = connection.execute(
            "SELECT status,attempts,completion_state,error_code FROM job_items WHERE job_id=?",
            (job_id,),
        ).fetchone()
        events = connection.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id=? AND event='timed_out_completed'",
            (job_id,),
        ).fetchone()[0]
    job = repository.get(job_id)
    assert calls == 1
    assert dict(item) == {
        "status": "failed",
        "attempts": 1,
        "completion_state": "timed_out_completed",
        "error_code": "JOB-004",
    }
    assert events == 1
    assert float(job["spent"]) == 0.25
    assert job["status"] == "completed_with_errors"
