from __future__ import annotations

import threading
import time

from inktime.app.workers.job_worker import BoundedJobWorker


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
