from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import inktime.app.repositories.resilience as resilience_module
from inktime.app.db import Database, migrate
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.repositories.resilience import ResilienceRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.services.budgets import BudgetService


def _seed_old_queue_item(
    database: Database,
    repository: ResilienceRepository,
    device_id: str,
    suffix: str,
    old_at: str,
) -> str:
    release_id = f"retention-release-{suffix}"
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO releases(id,display_type,width,height,pixel_format,manifest_json,status,created_at,published_at,created_by,render_profile) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                release_id,
                "image",
                480,
                800,
                "RGB",
                "{}",
                "published",
                old_at,
                old_at,
                "test",
                "safe_4c",
            ),
        )
    repository.ensure_queue(device_id)
    item = repository.enqueue_release(device_id=device_id, release_id=release_id)
    item_id = str(item["id"])
    with database.transaction() as connection:
        connection.execute(
            "UPDATE device_content_queue_items SET status='DISPLAYED',updated_at=?,terminal_ack_retention=? WHERE id=?",
            (old_at, old_at, item_id),
        )
        connection.execute(
            "UPDATE device_content_queues SET current_release_id=NULL,last_known_good_release_id=NULL,next_queued_release_id=NULL,emergency_fallback_release_id=NULL WHERE device_id=?",
            (device_id,),
        )
    return item_id


def _seed_queue_event(database: Database, item_id: str, device_id: str, old_at: str, suffix: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO device_content_queue_events(queue_item_id,device_id,event_type,idempotency_key,payload_json,created_at) VALUES (?,?,?,?,?,?)",
            (item_id, device_id, "DISPLAY_COMPLETED", f"retention-event-{suffix}", "{}", old_at),
        )


def test_queue_event_retention_fences_parent_gc_until_child_cleanup(tmp_path: Path):
    database = Database(tmp_path / "queue-event-retention.sqlite3")
    migrate(database)
    repository = ResilienceRepository(database)
    device_id, _token = DeviceRepository(database, "test-pepper").create("Queue retention")
    old_at = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    retained_item = _seed_old_queue_item(database, repository, device_id, "retained", old_at)
    _seed_queue_event(database, retained_item, device_id, old_at, "retained")

    repository.update_retention(
        "queue_event", {"retention_days": 180, "cleanup_batch_size": 200, "dry_run": False}
    )
    before_policy = repository.expire_operational_data()
    assert before_policy["gc_queue_items"] == 0
    with database.session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_items WHERE id=?", (retained_item,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_events WHERE queue_item_id=?", (retained_item,)
        ).fetchone()[0] == 1

    repository.update_retention(
        "queue_event", {"retention_days": 1, "cleanup_batch_size": 200, "dry_run": True}
    )
    dry_run = repository.cleanup(dry_run=False)
    assert dry_run["summary"]["queue_event"] == 1
    assert repository.expire_operational_data()["gc_queue_items"] == 0

    repository.update_retention(
        "queue_event", {"enabled": False, "retention_days": 1, "cleanup_batch_size": 200, "dry_run": False}
    )
    disabled = repository.cleanup(dry_run=False)
    assert disabled["summary"].get("queue_event", 0) == 0
    assert repository.expire_operational_data()["gc_queue_items"] == 0

    repository.update_retention(
        "queue_event", {"enabled": True, "retention_days": 1, "cleanup_batch_size": 200, "dry_run": False}
    )
    cleaned = repository.cleanup(dry_run=False)
    assert cleaned["summary"]["queue_event"] == 1
    with database.session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_events WHERE queue_item_id=?", (retained_item,)
        ).fetchone()[0] == 0

    no_child_item = _seed_old_queue_item(database, repository, device_id, "no-child", old_at)
    after_cleanup = repository.expire_operational_data()
    assert after_cleanup["gc_queue_items"] == 2
    with database.session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_items WHERE id IN (?,?)",
            (retained_item, no_child_item),
        ).fetchone()[0] == 0


def test_decision_trace_caps_candidate_detail_at_fifty(tmp_path: Path):
    database = Database(tmp_path / "inktime.sqlite3")
    migrate(database)
    repository = ResilienceRepository(database)
    version = repository.algorithm_version(
        name="selection",
        version="v1",
        configuration={"weight": 1},
        renderer="r1",
        layout="l1",
        pairing="p1",
        scoring="s1",
    )
    trace_id = repository.create_trace(
        execution_mode="test",
        algorithm_version_id=version,
        primary_photo_id=None,
        candidates=[{"adjusted_score": float(index)} for index in range(100)],
        candidate_count=100,
    )
    trace = repository.trace(trace_id)
    assert trace is not None
    assert len(trace["candidates"]) == 50
    assert trace["candidate_count"] == 100


def test_algorithm_version_uses_stable_configuration_hash(tmp_path: Path):
    database = Database(tmp_path / "inktime.sqlite3")
    migrate(database)
    repository = ResilienceRepository(database)
    first = repository.algorithm_version(
        name="selection",
        version="v1",
        configuration={"a": 1},
        renderer="r1",
        layout="l1",
        pairing="p1",
        scoring="s1",
    )
    second = repository.algorithm_version(
        name="selection",
        version="v1",
        configuration={"a": 1},
        renderer="r1",
        layout="l1",
        pairing="p1",
        scoring="s1",
    )
    assert first == second


def test_cleanup_honors_policy_dry_run_fence(tmp_path: Path):
    database = Database(tmp_path / "policy-dry-run.sqlite3")
    migrate(database)
    repository = ResilienceRepository(database)
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    repository.update_retention(
        "api_usage",
        {"retention_days": 1, "cleanup_batch_size": 1, "dry_run": True},
    )
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO api_usage(provider,model,request_type,estimated_cost,started_at,status,cost_source,image_bytes) "
            "VALUES ('provider','model','analysis',0.25,?,'failed','unknown',1)",
            ((month_start - timedelta(days=2)).isoformat(),),
        )

    result = repository.cleanup(dry_run=False)

    with database.session() as connection:
        remaining = connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0]
        item = connection.execute(
            "SELECT result FROM data_cleanup_items WHERE cleanup_run_id=? AND data_type='api_usage'",
            (result["id"],),
        ).fetchone()
    assert result["summary"]["api_usage"] == 1
    assert remaining == 1
    assert item["result"] == "planned"


def test_fresh_api_usage_default_deletes_in_bounded_restart_safe_batches(
    monkeypatch, tmp_path: Path
):
    database = Database(tmp_path / "fresh-api-usage-retention.sqlite3")
    migrate(database)
    repository = ResilienceRepository(database)
    fixed_now = datetime.now(timezone.utc)
    month_start = fixed_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(resilience_module, "datetime", FixedDateTime)
    stale = (fixed_now - timedelta(days=401)).isoformat()
    exact_cutoff = (fixed_now - timedelta(days=400)).isoformat()
    current_month = month_start.isoformat()
    today = fixed_now.isoformat()
    with database.transaction() as connection:
        policy = connection.execute(
            "SELECT enabled,retention_days,cleanup_batch_size,dry_run "
            "FROM data_retention_policies WHERE data_type='api_usage'"
        ).fetchone()
        connection.executemany(
            "INSERT INTO api_usage(provider,model,request_type,estimated_cost,started_at,status,cost_source,image_bytes) "
            "VALUES ('provider','model','stale',0.01,?,'completed','estimated',1)",
            [(stale,)] * 201,
        )
        connection.executemany(
            "INSERT INTO api_usage(provider,model,request_type,estimated_cost,started_at,status,cost_source,image_bytes) "
            "VALUES ('provider','model',?,?,?,'completed','estimated',1)",
            [
                ("exact-cutoff", 0.10, exact_cutoff),
                ("current-month", 0.20, current_month),
                ("today", 0.30, today),
            ],
        )
        query_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM api_usage "
            "WHERE started_at<? AND date(started_at)<date('now','start of month') "
            "ORDER BY started_at,id LIMIT ?",
            (exact_cutoff, 200),
        ).fetchall()
    assert tuple(policy) == (1, 400, 200, 0)
    assert any("idx_api_usage_time" in str(row["detail"]) for row in query_plan)

    budget = BudgetService(database, SettingsRepository(database))
    budget_before = budget.snapshot()
    first = repository.cleanup(dry_run=False)
    budget_after_first = budget.snapshot()
    second = repository.cleanup(dry_run=False)
    third = repository.cleanup(dry_run=False)

    assert first["summary"]["api_usage"] == 200
    assert second["summary"]["api_usage"] == 1
    assert third["summary"]["api_usage"] == 0
    assert budget_after_first["daily_known"] == budget_before["daily_known"]
    assert budget_after_first["monthly_known"] == budget_before["monthly_known"]
    with database.session() as connection:
        remaining = connection.execute(
            "SELECT request_type,started_at FROM api_usage ORDER BY id"
        ).fetchall()
        deleted_first = connection.execute(
            "SELECT COUNT(*) FROM data_cleanup_items "
            "WHERE cleanup_run_id=? AND data_type='api_usage' AND result='deleted'",
            (first["id"],),
        ).fetchone()[0]
        statuses = [
            str(row["status"])
            for row in connection.execute(
                "SELECT status FROM data_cleanup_runs WHERE id IN (?,?,?) ORDER BY started_at,id",
                (first["id"], second["id"], third["id"]),
            ).fetchall()
        ]
    assert deleted_first == 200
    assert [(str(row["request_type"]), str(row["started_at"])) for row in remaining] == [
        ("exact-cutoff", exact_cutoff),
        ("current-month", current_month),
        ("today", today),
    ]
    assert statuses == ["completed", "completed", "completed"]


def test_api_usage_cleanup_is_bounded_restart_safe_and_observable(tmp_path: Path):
    database = Database(tmp_path / "api-usage-retention.sqlite3")
    migrate(database)
    repository = ResilienceRepository(database)
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    old_rows = [
        (month_start - timedelta(days=4), "completed", "estimated", 0.10),
        (month_start - timedelta(days=3), "failed", "unknown", 0.05),
        (month_start - timedelta(days=2), "completed", "unknown", 0.25),
        (month_start - timedelta(days=1), "failed", "unknown", 0.50),
    ]
    current_month_old = (month_start + timedelta(hours=1), "completed", "unknown", 0.75)
    recent = (now - timedelta(hours=1)).isoformat()
    repository.update_retention(
        "api_usage", {"retention_days": 1, "cleanup_batch_size": 2, "dry_run": False}
    )
    with database.transaction() as connection:
        connection.executemany(
            "INSERT INTO api_usage(provider,model,request_type,estimated_cost,started_at,status,cost_source,image_bytes) "
            "VALUES ('provider','model','analysis',?,?,?,?,1)",
            [(cost, started.isoformat(), status, cost_source) for started, status, cost_source, cost in old_rows]
            + [(cost, started.isoformat(), status, cost_source) for started, status, cost_source, cost in [current_month_old]]
            + [(0.01, recent, "completed", "estimated")],
        )

    with database.session() as connection:
        old_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM api_usage WHERE started_at<? AND date(started_at)<date('now','start of month') ORDER BY started_at,id",
                ((now - timedelta(days=1)).isoformat(),),
            ).fetchall()
        ]
    assert len(old_ids) == 4

    dry_run = repository.cleanup(dry_run=True)
    assert dry_run["summary"]["api_usage"] == 2
    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 6
        run = connection.execute(
            "SELECT status,summary_json FROM data_cleanup_runs WHERE id=?", (dry_run["id"],)
        ).fetchone()
        planned = connection.execute(
            "SELECT reference_id,result FROM data_cleanup_items WHERE cleanup_run_id=? AND data_type='api_usage' ORDER BY id",
            (dry_run["id"],),
        ).fetchall()
    assert run["status"] == "completed"
    assert json.loads(run["summary_json"])["api_usage"] == 2
    assert [(str(row["reference_id"]), row["result"]) for row in planned] == [
        (old_ids[0], "planned"),
        (old_ids[1], "planned"),
    ]

    first = repository.cleanup(dry_run=False)
    second = repository.cleanup(dry_run=False)
    third = repository.cleanup(dry_run=False)
    assert first["summary"]["api_usage"] == 2
    assert second["summary"]["api_usage"] == 2
    assert third["summary"]["api_usage"] == 0

    with database.session() as connection:
        remaining = connection.execute("SELECT started_at,status,cost_source FROM api_usage").fetchall()
        deleted = connection.execute(
            "SELECT COUNT(*) FROM data_cleanup_items WHERE cleanup_run_id=? AND data_type='api_usage' AND result='deleted'",
            (first["id"],),
        ).fetchone()[0]
        run_statuses = [
            row["status"]
            for row in connection.execute(
                "SELECT status FROM data_cleanup_runs WHERE id IN (?,?,?) ORDER BY started_at",
                (first["id"], second["id"], third["id"]),
            ).fetchall()
        ]
    assert len(remaining) == 2
    assert {row["started_at"] for row in remaining} == {current_month_old[0].isoformat(), recent}
    assert {row["cost_source"] for row in remaining} == {"unknown", "estimated"}
    assert deleted == 2
    assert run_statuses == ["completed", "completed", "completed"]


def test_cleanup_failure_after_first_policy_commit_is_observable_and_retryable(tmp_path: Path, monkeypatch):
    database = Database(tmp_path / "api-usage-retention-failure.sqlite3")
    migrate(database)
    repository = ResilienceRepository(database)
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    repository.update_retention(
        "api_usage", {"retention_days": 1, "cleanup_batch_size": 2, "dry_run": False}
    )
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO api_usage(provider,model,request_type,estimated_cost,started_at,status,cost_source,image_bytes) "
            "VALUES ('provider','model','analysis',0.25,?,'failed','unknown',1)",
            ((month_start - timedelta(days=2)).isoformat(),),
        )

    original_transaction = database.transaction
    policy_transaction_count = 0

    def transaction_with_fault(*, immediate=True, operation="repository_write"):
        nonlocal policy_transaction_count
        if operation == "retention_cleanup_policy":
            policy_transaction_count += 1
            if policy_transaction_count == 2:
                raise RuntimeError("injected later policy failure")
        return original_transaction(immediate=immediate, operation=operation)

    monkeypatch.setattr(database, "transaction", transaction_with_fault)
    with pytest.raises(RuntimeError, match="injected later policy failure"):
        repository.cleanup(dry_run=False)
    monkeypatch.setattr(database, "transaction", original_transaction)

    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0
        failed_run = connection.execute(
            "SELECT status,summary_json,error_code FROM data_cleanup_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM data_cleanup_items WHERE data_type='api_usage' AND result='deleted'"
        ).fetchone()[0] == 1
    assert failed_run["status"] == "failed"
    assert json.loads(failed_run["summary_json"])["api_usage"] == 1
    assert failed_run["error_code"] == "RETENTION-CLEANUP-FAILED"

    retry = repository.cleanup(dry_run=False)
    assert retry["summary"]["api_usage"] == 0
    with database.session() as connection:
        assert connection.execute(
            "SELECT status FROM data_cleanup_runs WHERE id=?", (retry["id"],)
        ).fetchone()[0] == "completed"
