from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from inktime.app.services.jobs import JobService
from inktime.app.workers.job_worker import BoundedJobWorker


def add_photos(app, count: int) -> list[str]:
    database = app.extensions["inktime_database"]
    now = datetime.now(timezone.utc).isoformat()
    library_id = str(uuid4())
    photo_ids = [str(uuid4()) for _ in range(count)]
    with database.session() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
            (library_id, "測試相簿", "/photos", now, now),
        )
        connection.executemany(
            """
            INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at)
            VALUES (?,?,?,'discovered',?,?)
            """,
            [(photo_id, library_id, f"{index}.jpg", now, now) for index, photo_id in enumerate(photo_ids)],
        )
    return photo_ids


def create_job(app, count: int = 10):
    photo_ids = add_photos(app, count)
    service: JobService = app.extensions["inktime_job_service"]
    job_id = service.create_analysis_job(
        name="測試工作",
        strategy="local",
        settings={},
        created_by="tester",
        budget_limit=None,
        photo_ids=iter(photo_ids),
    )
    return service, app.extensions["inktime_job_repository"], job_id


def test_pause_resume_cancel_state_machine(app):
    service, repository, job_id = create_job(app, 3)
    service.start(job_id)
    service.pause(job_id)
    worker = BoundedJobWorker(repository, lambda item: {"ok": True})
    worker.run_job(job_id)
    assert repository.get(job_id)["status"] == "paused"
    service.resume(job_id)
    worker.run_job(job_id)
    assert repository.get(job_id)["status"] == "completed"

    _, repository, cancelled_id = create_job(app, 3)
    service.start(cancelled_id)
    service.cancel(cancelled_id)
    called = 0

    def processor(item):
        nonlocal called
        called += 1
        return {}

    BoundedJobWorker(repository, processor).run_job(cancelled_id)
    assert called == 0
    assert repository.get(cancelled_id)["status"] == "cancelled"


def test_worker_never_submits_all_items_at_once(app):
    service, repository, job_id = create_job(app, 250)
    service.start(job_id)
    worker = BoundedJobWorker(
        repository, lambda item: {"photo_id": item["photo_id"]}, concurrency=4, queue_multiplier=2
    )
    worker.run_job(job_id)
    assert worker.max_observed_futures <= 8
    job = repository.get(job_id)
    assert job["status"] == "completed"
    assert job["completed_items"] == 250


def test_completed_item_records_actual_processing_stage(app):
    service, repository, job_id = create_job(app, 1)
    service.start(job_id)
    BoundedJobWorker(repository, lambda item: {"stage": "prefilter", "saved_tokens": True}).run_job(
        job_id
    )

    item = repository.list_items(job_id)[0]
    assert item["status"] == "completed"
    assert item["stage"] == "prefilter"


def test_stale_running_items_are_recovered_after_restart(app):
    service, repository, job_id = create_job(app, 1)
    service.start(job_id)
    claimed = repository.claim(job_id, "dead-worker", 1, lease_seconds=300)
    assert claimed
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE job_items SET lease_until=? WHERE id=?", (expired, claimed[0]["id"]))
    assert repository.recover_stale() == 1
    BoundedJobWorker(repository, lambda item: {"recovered": True}).run_job(job_id)
    assert repository.get(job_id)["status"] == "completed"


def test_failed_items_can_be_retried(app):
    service, repository, job_id = create_job(app, 1)
    service.start(job_id)
    worker = BoundedJobWorker(
        repository, lambda item: (_ for _ in ()).throw(RuntimeError("失敗")), max_attempts=1
    )
    worker.run_job(job_id)
    assert repository.get(job_id)["status"] == "completed_with_errors"
    assert service.retry_failed(job_id) == 1
    service.start(job_id)
    BoundedJobWorker(repository, lambda item: {"ok": True}).run_job(job_id)
    assert repository.get(job_id)["status"] == "completed"


def test_budget_block_returns_item_and_pauses_new_work(app):
    class BudgetBlocked(RuntimeError):
        code = "BUDGET-001"

    service, repository, job_id = create_job(app, 1)
    service.start(job_id)
    BoundedJobWorker(repository, lambda item: (_ for _ in ()).throw(BudgetBlocked("已達上限"))).run_job(
        job_id
    )
    assert repository.get(job_id)["status"] == "budget_exceeded"
    item = repository.list_items(job_id)[0]
    assert item["status"] == "pending"
    assert item["attempts"] == 0


def test_keyset_queue_reaches_old_job_after_more_than_one_hundred(app):
    repository = app.extensions["inktime_job_repository"]
    service = app.extensions["inktime_job_service"]
    job_ids = []
    for index in range(101):
        job_id = repository.create_maintenance(
            kind="cleanup", name=f"清理 {index}", settings={}, created_by="tester", priority=6
        )
        service.start(job_id)
        job_ids.append(job_id)
    runnable = list(repository.iter_runnable())
    assert len(runnable) == 101
    assert str(runnable[-1]["id"]) == job_ids[-1]


def test_active_dedupe_key_prevents_duplicate_maintenance_work(app):
    repository = app.extensions["inktime_job_repository"]
    first = repository.create_maintenance(
        kind="cleanup", name="快取清理", settings={}, created_by="tester", dedupe_key="scheduled:cache_cleanup"
    )
    second = repository.create_maintenance(
        kind="cleanup", name="快取清理", settings={}, created_by="tester", dedupe_key="scheduled:cache_cleanup"
    )
    assert second == first
