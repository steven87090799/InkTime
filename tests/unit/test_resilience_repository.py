from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from inktime.app.db import Database, migrate
from inktime.app.repositories.resilience import ResilienceRepository


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
