from __future__ import annotations

import threading
import time
import os
from pathlib import Path

from inktime.app.workers.job_worker import BoundedJobWorker


def test_on_time_futures_remain_successful_when_supervisor_observes_them_late(app, monkeypatch):
    from concurrent.futures import ALL_COMPLETED, wait as real_wait
    from types import SimpleNamespace
    import inktime.app.workers.job_worker as worker_module
    from tests.integration.test_jobs import add_photos

    repository = app.extensions["inktime_job_repository"]
    job_id = repository.create(name="delayed bookkeeping", strategy="local", settings={},
                               photo_ids=add_photos(app, 2), created_by="test")
    app.extensions["inktime_job_service"].start(job_id)
    clock = [0.0]
    monkeypatch.setattr(worker_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def observe_after_deadline(futures, **_kwargs):
        done, pending = real_wait(futures, timeout=5, return_when=ALL_COMPLETED)
        assert len(done) == 2 and not pending
        clock[0] = 2.0
        return done, pending
    monkeypatch.setattr(worker_module, "wait", observe_after_deadline)
    worker = BoundedJobWorker(repository, lambda item: {"photo_id": item["photo_id"]},
                              concurrency=2, queue_multiplier=1, timeout_seconds=1)
    worker.run_job(job_id)
    assert repository.get(job_id)["status"] == "completed"
    with repository.database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_items WHERE job_id=? AND status='completed'",
                                  (job_id,)).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM job_items WHERE job_id=? AND dead_lettered_at IS NOT NULL",
                                  (job_id,)).fetchone()[0] == 0


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


def test_late_transport_failure_is_not_put_back_on_retry_queue(app):
    repository = app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(kind="cleanup", name="late-failure", settings={}, created_by="test")
    app.extensions["inktime_job_service"].start(job_id)
    calls = []
    def late_failure(_item):
        calls.append(True)
        time.sleep(1.2)
        raise TimeoutError("response lost after request may have arrived")
    worker = BoundedJobWorker(repository, late_failure, concurrency=1, queue_multiplier=1, timeout_seconds=1)
    worker.run_job(job_id)
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute("SELECT status,attempts,error_code FROM job_items WHERE job_id=?", (job_id,)).fetchone()
    assert len(calls) == 1
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert row["error_code"] == "JOB-SHUTDOWN-AMBIGUOUS"
