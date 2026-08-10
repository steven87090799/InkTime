from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from inktime.app.core.idempotency import request_fingerprint, scoped_idempotency_key
from inktime.app.repositories.jobs import PreviewCapacityError
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


def create_job(
    app,
    count: int = 10,
    *,
    dedupe_key: str | None = None,
    request_fingerprint_value: str | None = None,
):
    photo_ids = add_photos(app, count)
    service: JobService = app.extensions["inktime_job_service"]
    job_id = service.create_analysis_job(
        name="測試工作",
        strategy="local",
        settings={},
        created_by="tester",
        budget_limit=None,
        photo_ids=iter(photo_ids),
        dedupe_key=dedupe_key,
        request_fingerprint=request_fingerprint_value,
    )
    return service, app.extensions["inktime_job_repository"], job_id


def test_request_level_idempotency_ledger_is_atomic_replayable_and_conflict_safe(app):
    repository = app.extensions["inktime_job_repository"]
    scope = scoped_idempotency_key("test-ledger", "tester", "same-key")
    first = repository.reserve_idempotent_request(
        scope, "fingerprint-a", {"batches": [{"group": "a", "photo_ids": ["photo-a"]}]}
    )
    assert first["status"] == "in_progress"

    concurrent_scope = scoped_idempotency_key("test-ledger", "tester", "concurrent-key")
    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent = list(
            pool.map(
                lambda _index: repository.reserve_idempotent_request(
                    concurrent_scope, "fingerprint-c", {"batches": []}
                ),
                range(2),
            )
        )
    assert {row["status"] for row in concurrent} == {"in_progress"}
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM idempotency_requests WHERE scope_key=?", (concurrent_scope,)
        ).fetchone()[0] == 1

    completed = repository.complete_idempotent_request(
        scope,
        "fingerprint-a",
        {"jobs": [{"id": "job-a"}], "queued": 1, "batch_by": "folder"},
    )
    assert completed["status"] == "completed"
    replay = repository.reserve_idempotent_request(scope, "fingerprint-a", {"batches": []})
    assert replay["response_json"] == completed["response_json"]
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        repository.reserve_idempotent_request(scope, "fingerprint-b", {"batches": []})
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM idempotency_requests WHERE scope_key=?", (scope,)
        ).fetchone()[0] == 1


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
    BoundedJobWorker(repository, lambda item: {"stage": "prefilter", "saved_tokens": True}).run_job(job_id)

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


def test_item_completion_and_failure_require_current_worker_ownership(app):
    service, repository, job_id = create_job(app, 1)
    service.start(job_id)
    claimed = repository.claim(job_id, "worker-a", 1)
    item_id = str(claimed[0]["id"])

    assert not repository.complete_item(job_id, item_id, {"wrong": True}, worker_id="worker-b")
    assert not repository.fail_item(
        job_id,
        item_id,
        "JOB-003",
        "stale worker",
        max_attempts=1,
        worker_id="worker-b",
    )
    assert repository.list_items(job_id)[0]["status"] == "running"
    assert repository.complete_item(job_id, item_id, {"ok": True}, worker_id="worker-a")


def test_stale_recovery_dead_letters_after_bounded_attempts(app):
    service, repository, job_id = create_job(app, 1)
    service.start(job_id)
    claimed = repository.claim(job_id, "dead-worker", 1)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE job_items SET attempts=5,lease_until=? WHERE id=?",
            (expired, claimed[0]["id"]),
        )

    assert repository.recover_stale() == 1
    item = repository.list_items(job_id)[0]
    assert item["status"] == "failed"
    assert item["error_code"] == "WORKER_CRASH"


def test_recover_stale_does_not_take_writer_lock_without_candidates(app):
    database = app.extensions["inktime_database"]
    before = int(database.observability()["writer_lock_acquisitions"])

    assert app.extensions["inktime_job_repository"].recover_stale() == 0

    after = int(database.observability()["writer_lock_acquisitions"])
    assert after == before


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


def test_retry_failed_does_not_revive_running_or_cancelled_jobs(app):
    service, repository, running_id = create_job(app, 1)
    service.start(running_id)
    assert service.retry_failed(running_id) == 0
    assert repository.get(running_id)["status"] == "running"

    _, repository, cancelled_id = create_job(app, 1)
    service.start(cancelled_id)
    service.cancel(cancelled_id)
    assert service.retry_failed(cancelled_id) == 0
    assert repository.get(cancelled_id)["status"] == "cancelled"


def test_explicit_analysis_idempotency_key_reuses_terminal_job(app):
    dedupe_key = scoped_idempotency_key("analysis", "tester", "test-key")
    request_fp = request_fingerprint({"name": "測試工作", "strategy": "local", "settings": {}})
    service, repository, first_id = create_job(
        app, 1, dedupe_key=dedupe_key, request_fingerprint_value=request_fp
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE jobs SET status='completed',completed_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), first_id))
    replay_id = service.create_analysis_job(
        name="測試工作",
        strategy="local",
        settings={},
        created_by="tester",
        budget_limit=None,
        photo_ids=[],
        dedupe_key=dedupe_key,
        request_fingerprint=request_fp,
    )
    assert replay_id == first_id

    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        service.create_analysis_job(
            name="測試工作",
            strategy="local",
            settings={"different": True},
            created_by="tester",
            budget_limit=None,
            photo_ids=[],
            dedupe_key=dedupe_key,
            request_fingerprint=request_fingerprint({"different": True}),
        )

    other_actor_key = scoped_idempotency_key("analysis", "other-user", "test-key")
    other_actor_id = service.create_analysis_job(
        name="另一位使用者的工作",
        strategy="local",
        settings={},
        created_by="other-user",
        budget_limit=None,
        photo_ids=[],
        dedupe_key=other_actor_key,
        request_fingerprint=request_fp,
    )
    assert other_actor_id != first_id


def test_legacy_idempotency_row_binds_first_replay_fingerprint(app):
    dedupe_key = scoped_idempotency_key("analysis", "legacy-tester", "legacy-key")
    service, _repository, first_id = create_job(app, 1, dedupe_key=dedupe_key)
    request_fp = request_fingerprint({"name": "legacy", "strategy": "local", "settings": {}})

    replay_id = service.create_analysis_job(
        name="legacy",
        strategy="local",
        settings={},
        created_by="legacy-tester",
        budget_limit=None,
        photo_ids=[],
        dedupe_key=dedupe_key,
        request_fingerprint=request_fp,
    )

    assert replay_id == first_id
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute("SELECT request_fingerprint FROM jobs WHERE id=?", (first_id,)).fetchone()
    assert row["request_fingerprint"] == request_fp
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        service.create_analysis_job(
            name="legacy-different",
            strategy="local",
            settings={},
            created_by="legacy-tester",
            budget_limit=None,
            photo_ids=[],
            dedupe_key=dedupe_key,
            request_fingerprint=request_fingerprint({"different": True}),
        )


def test_scheduled_retry_wait_is_persisted_and_not_claimed_early(app):
    service, repository, job_id = create_job(app, 1)
    service.start(job_id)
    claimed = repository.claim(job_id, "scheduled-worker", 1)
    assert claimed

    repository.fail_item(
        job_id,
        str(claimed[0]["id"]),
        "NETWORK_TIMEOUT",
        "temporary outage",
        max_attempts=2,
        retry_interval_seconds=600,
    )

    item = repository.list_items(job_id)[0]
    assert item["status"] == "pending"
    available_at = datetime.fromisoformat(str(item["available_at"]))
    assert (available_at - datetime.now(timezone.utc)).total_seconds() > 590
    assert list(repository.iter_runnable()) == []


def test_structured_no_content_is_completed_without_failure_code(app):
    service, repository, job_id = create_job(app, 1)
    service.start(job_id)
    claimed = repository.claim(job_id, "worker", 1)
    assert claimed

    repository.complete_item(
        job_id,
        str(claimed[0]["id"]),
        {
            "status": "completed",
            "outcome": "no_content",
            "outcome_code": "NO_CONTENT",
            "error_code": "NO_CONTENT",
            "output_count": 0,
        },
    )
    assert repository.finalize_if_done(job_id)

    item = repository.list_items(job_id)[0]
    job = repository.get(job_id)
    assert item["status"] == "completed"
    assert item["error_code"] is None
    assert repository.outcome_codes(job_id) == ["NO_CONTENT"]
    assert job["status"] == "completed"
    assert job["failed_items"] == 0


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


def test_analysis_selector_keyset_returns_1200_unique_eligible_active_photos(app):
    photo_ids = add_photos(app, 1200)
    repository = app.extensions["inktime_job_repository"]
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET local_candidate_score=CAST(substr(relative_path,1,instr(relative_path,'.')-1) AS REAL) % 101"
        )
        connection.execute("UPDATE photos SET eligible=0 WHERE id=?", (photo_ids[0],))
        connection.execute("UPDATE photos SET lifecycle_status='missing' WHERE id=?", (photo_ids[1],))
    preview = repository.selection_preview(analysis_fingerprint="plan-a", limit=None)
    selected = list(repository.iter_pending_photo_ids(analysis_fingerprint="plan-a", limit=None))
    assert preview["total_active"] == 1199
    assert preview["excluded"] == 1
    assert preview["missing"] == 1
    assert preview["pending_total"] == 1198
    assert len(selected) == 1198
    assert len(set(selected)) == 1198
    assert photo_ids[0] not in selected and photo_ids[1] not in selected
    with app.extensions["inktime_database"].session() as connection:
        expected = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM photos WHERE lifecycle_status='active' AND eligible=1 "
                "ORDER BY COALESCE(local_candidate_score,-1) DESC,id ASC"
            )
        ]
    assert selected == expected
    assert list(repository.iter_pending_photo_ids(analysis_fingerprint="plan-a", limit=137)) == expected[:137]


def test_active_dedupe_key_prevents_duplicate_maintenance_work(app):
    repository = app.extensions["inktime_job_repository"]
    first = repository.create_maintenance(
        kind="cleanup",
        name="快取清理",
        settings={},
        created_by="tester",
        dedupe_key="scheduled:cache_cleanup",
    )
    second = repository.create_maintenance(
        kind="cleanup",
        name="快取清理",
        settings={},
        created_by="tester",
        dedupe_key="scheduled:cache_cleanup",
    )
    assert second == first
    with app.extensions["inktime_database"].session() as connection:
        created = connection.execute(
            "SELECT message FROM job_events WHERE job_id=? AND event='created'",
            (first,),
        ).fetchall()
    assert [str(row["message"]) for row in created] == ["已建立 cleanup 維護工作"]


def test_each_maintenance_job_has_one_kind_specific_created_event(app):
    repository = app.extensions["inktime_job_repository"]
    for kind in ("scan", "backup", "webhook", "render_preview"):
        job_id = repository.create_maintenance(
            kind=kind,
            name=f"{kind} test",
            settings={},
            created_by="tester",
        )
        with app.extensions["inktime_database"].session() as connection:
            rows = connection.execute(
                "SELECT message FROM job_events WHERE job_id=? AND event='created'",
                (job_id,),
            ).fetchall()
        assert [str(row["message"]) for row in rows] == [f"已建立 {kind} 維護工作"]


def test_preview_capacity_failure_leaves_no_job_item_or_event(app):
    repository = app.extensions["inktime_job_repository"]
    owner_job_id = repository.create_maintenance_with_capacity(
        kind="render_preview",
        name="capacity owner",
        settings={},
        created_by="same-user",
        priority=6,
        per_user_limit=1,
        system_limit=8,
    )
    with app.extensions["inktime_database"].session() as connection:
        created = connection.execute(
            "SELECT message FROM job_events WHERE job_id=? AND event='created'",
            (owner_job_id,),
        ).fetchall()
        before = tuple(
            int(value)
            for value in connection.execute(
                "SELECT (SELECT COUNT(*) FROM jobs),"
                "(SELECT COUNT(*) FROM job_items),"
                "(SELECT COUNT(*) FROM job_events)"
            ).fetchone()
        )
    assert [str(row["message"]) for row in created] == ["已建立 render_preview 維護工作"]
    with pytest.raises(PreviewCapacityError):
        repository.create_maintenance_with_capacity(
            kind="render_preview",
            name="must roll back",
            settings={},
            created_by="same-user",
            priority=6,
            per_user_limit=1,
            system_limit=8,
        )
    with app.extensions["inktime_database"].session() as connection:
        after = tuple(
            int(value)
            for value in connection.execute(
                "SELECT (SELECT COUNT(*) FROM jobs),"
                "(SELECT COUNT(*) FROM job_items),"
                "(SELECT COUNT(*) FROM job_events)"
            ).fetchone()
        )
        leaked = connection.execute("SELECT COUNT(*) FROM jobs WHERE name='must roll back'").fetchone()[0]
    assert after == before
    assert int(leaked) == 0
