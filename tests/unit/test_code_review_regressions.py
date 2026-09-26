"""Regression tests for the defects found in the 2026-09 production readiness review.

Includes the original remediation checks and follow-up upgrade/recovery
regressions. Hosted CI is the execution authority for these tests.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json

import pytest

from inktime.app.api.operations import SCAN_TIMEOUT_SECONDS
from inktime.app.domain.analysis.schema import (
    PROTOCOL_ENUM_VALUES,
    AnalysisValidationError,
    validate_model_response,
)
from inktime.app.domain.analysis.traditional_chinese import to_taiwan_traditional
from inktime.app.domain.jobs.failure_policy import FailureClass, classify_failure
from inktime.app.providers.config import provider_revision
from inktime.app.services.local_selection import LocalSelectionPolicy


def _analysis_payload(**overrides) -> dict:
    payload = {
        "schema_version": 5,
        "types": ["文件"],
        "memory_score": 45,
        "visual_score": 50,
        "special_level": 0,
        "side_caption": "紙上還留著昨天的字",
        "content_filter": {
            code: {"detected": False, "confidence": 0.1}
            for code in ("explicit_nudity", "female_glamour_portrait", "sexualized_content")
        },
        "visual_orientation": {
            "rotation_cw": 0,
            "confidence": 0.9,
            "ambiguous": False,
            "evidence": ["text_upright"],
        },
    }
    payload.update(overrides)
    return payload


# ISSUE-001 -- s2twp rewrote the enum member 文件 to 檔案 before the enum check.
def test_document_type_enum_survives_traditional_chinese_conversion():
    result = validate_model_response(json.dumps(_analysis_payload(), ensure_ascii=False))
    assert result["types"] == ["文件"]


def test_protocol_enum_values_are_never_converted():
    # Without the protected set this rewrites 文件 -> 檔案.
    assert to_taiwan_traditional("文件", protected=PROTOCOL_ENUM_VALUES) == "文件"
    # Prose is still converted; the guard must not disable conversion wholesale.
    assert to_taiwan_traditional({"side_caption": "軟件"}, protected=PROTOCOL_ENUM_VALUES) != {
        "side_caption": "軟件"
    }


def test_unknown_type_is_still_rejected():
    with pytest.raises(AnalysisValidationError):
        validate_model_response(json.dumps(_analysis_payload(types=["檔案"]), ensure_ascii=False))


# ISSUE-002 -- routing-only provider fields were part of the paid-cache identity.
def test_routing_only_provider_fields_do_not_change_semantic_revision():
    base = {
        "id": "p1",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
        "options": {},
        "supports_vision": True,
        "supports_json_schema": True,
        "supports_batch": False,
        "priority": 10,
    }
    reordered = {**base, "priority": 20}
    batch_enabled = {**base, "supports_batch": True}
    assert provider_revision(base, semantic=True) == provider_revision(reordered, semantic=True)
    assert provider_revision(base, semantic=True) == provider_revision(batch_enabled, semantic=True)


def test_wire_affecting_provider_fields_still_change_semantic_revision():
    base = {"id": "p1", "kind": "openai", "base_url": "https://a/v1", "model": "m1", "options": {}}
    assert provider_revision(base, semantic=True) != provider_revision(
        {**base, "model": "m2"}, semantic=True
    )
    assert provider_revision(base, semantic=True) != provider_revision(
        {**base, "base_url": "https://b/v1"}, semantic=True
    )


def test_operational_fields_remain_excluded_from_semantic_revision():
    base = {"id": "p1", "kind": "openai", "base_url": "https://a/v1", "model": "m1", "options": {}}
    assert provider_revision(base, semantic=True) == provider_revision(
        {**base, "timeout_seconds": 90, "rate_limit_rpm": 30}, semantic=True
    )


# ISSUE-003 -- API scans inherited the 900s process-boundary default.
def test_api_scan_modes_all_carry_an_explicit_walk_budget():
    from inktime.app.workers.scanner import SCAN_MODES

    assert set(SCAN_MODES) <= set(SCAN_TIMEOUT_SECONDS)
    # The worker falls back to 900s when the key is absent; every mode must beat it.
    assert all(value > 900 for value in SCAN_TIMEOUT_SECONDS.values())
    assert SCAN_TIMEOUT_SECONDS["full"] >= SCAN_TIMEOUT_SECONDS["incremental"]


def test_enqueue_scan_persists_the_walk_budget(app, client):
    from tests.conftest import create_admin, csrf, login

    create_admin(app)
    login(client)
    photo_dir = app.extensions["inktime_runtime_config"].photo_dir
    photo_dir.mkdir(parents=True, exist_ok=True)
    response = client.post(
        "/api/v1/maintenance/scan",
        json={"root_path": str(photo_dir), "mode": "incremental", "build_thumbnails": False},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code in {200, 201, 202}, response.get_data(as_text=True)
    job_id = response.get_json()["id"]
    job = app.extensions["inktime_job_repository"].get(job_id)
    settings = json.loads(str(job["settings_json"]))
    assert int(settings["timeout_seconds"]) == SCAN_TIMEOUT_SECONDS["incremental"]


# ISSUE-004 -- graceful shutdown dead-lettered re-runnable local work.
def test_shutdown_cancellation_is_retryable_not_terminal():
    class _Cancelled(RuntimeError):
        code = "JOB-SHUTDOWN-CANCELLED"

    assert classify_failure(_Cancelled()) is FailureClass.RETRYABLE


def test_genuine_local_failure_remains_terminal():
    class _Failed(RuntimeError):
        code = "JOB-LOCAL-FAILED"

    assert classify_failure(_Failed()) is FailureClass.TERMINAL_NO_RETRY


def test_process_boundary_marks_only_cancellation():
    from inktime.app.workers.process_boundary import ProcessCallError

    assert ProcessCallError("boom").cancelled is False
    flagged = ProcessCallError("cancelled")
    flagged.cancelled = True
    assert flagged.cancelled is True


# ISSUE-005 -- pair layout could emit the same photo twice.
def _ranked_row(identifier: str, score: float, **extra) -> dict:
    """A row shaped the way LocalSelectionPolicy.ranked() emits it."""

    row = {
        "id": identifier,
        "local_display_score": score,
        "local_candidate_score": score,
        "width": 1200,
        "height": 800,
        "captured_at": "2024-05-01T10:00:00+00:00",
        "captured_date": "2024-05-01",
        "captured_month_day": "05-01",
        "score_components": {"recent_display_penalty": 0.0},
        "city": "",
        "local_quality_decision": "eligible",
        "epaper_contrast_risk": "low",
        "perceptual_hash": None,
        "difference_hash": None,
        "duplicate_group_id": None,
        "burst_group_id": None,
        "sha256": identifier * 8,
        "gps_lat": None,
        "gps_lon": None,
    }
    row.update(extra)
    return row


def test_pair_selection_never_repeats_a_photo(app, monkeypatch):
    """Drive the real select() pair branch and assert no photo is emitted twice.

    `secondary` is chosen from the whole allowed pool, so it can already sit
    later in `selected`.  Before the fix the branch spliced it in with
    `[primary, secondary] + selected[2:]`, which both duplicated that photo and
    silently discarded the original `selected[1]`.
    """

    policy = LocalSelectionPolicy(
        app.extensions["inktime_database"],
        app.extensions["inktime_settings_repository"],
    )
    # Portrait `b` outranks the landscape candidates, so the pair branch prefers
    # the opposite-orientation partner already present further down the list.
    ranked = [
        _ranked_row("a", 90.0, width=1200, height=800),
        _ranked_row("b", 80.0, width=800, height=1200),
        _ranked_row("c", 70.0, width=1200, height=800),
        _ranked_row("d", 60.0, width=1200, height=800),
    ]
    monkeypatch.setattr(
        LocalSelectionPolicy,
        "ranked",
        lambda self, *, target, orientation, limit=None, excluded_ids=None: [
            dict(row) for row in ranked
        ],
    )

    result = policy.select(
        target=date(2024, 5, 1),
        orientation="landscape",
        quantity=3,
        layout="photo_pair",
    )
    identifiers = [str(row["id"]) for row in result["selected"]]
    assert identifiers, "expected the policy to select photos"
    assert len(identifiers) == len(set(identifiers)), f"duplicate photo in one release: {identifiers}"


def test_stale_budget_reservations_require_settlement_evidence(app):
    from inktime.app.repositories.usage import UsageRepository

    budgets = app.extensions["inktime_budget_service"]
    database = app.extensions["inktime_database"]
    stale = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    operations = {
        "started": "started", "saved": "response", "unknown": "completed",
        "settled": "completed", "zero": "completed", "not-sent": "not_sent",
        "saved-accounted": "response",
    }
    with database.transaction() as connection:
        for name, state in operations.items():
            connection.execute(
                "INSERT INTO billable_operations(id,content_sha256,request_fingerprint,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (name, name, name, state, stale, stale),
            )
        for name in (*("vision:" + key for key in operations), "batch:unimported", "diagnostic-unknown"):
            connection.execute(
                "INSERT INTO budget_reservations(id,amount,state,created_at) VALUES (?,1,'active',?)",
                (name, stale),
            )
        connection.execute(
            "INSERT INTO budget_reservations(id,amount,state,created_at) VALUES ('fresh',1,'active',?)",
            (fresh,),
        )
    usage = UsageRepository(database)
    for name, cost in (("unknown", None), ("settled", 1.0), ("zero", 0.0), ("saved-accounted", 0.5)):
        usage.record(
            provider="test", model="test", job_id=None, photo_id=None,
            request_type="vision", input_tokens=0, output_tokens=0, cached_tokens=0,
            estimated_cost=None, actual_cost=cost, started_at=stale, latency_ms=1,
            status="completed", operation_id=name, tokens_reported=cost is not None,
            cost_source="unknown" if cost is None else "provider_reported",
        )
    assert budgets.snapshot()["reserved"] == pytest.approx(10.0)
    assert budgets.expire_stale_reservations() == 4
    assert budgets.snapshot()["reserved"] == pytest.approx(6.0)
    assert budgets.expire_stale_reservations() == 0
    with database.session() as connection:
        released = {row[0] for row in connection.execute(
            "SELECT id FROM budget_reservations WHERE state='released'"
        )}
    assert released == {"vision:settled", "vision:zero", "vision:not-sent", "vision:saved-accounted"}


# ISSUE-017 -- the CI paid-state fixture seeded a reservation with a hard-coded
# date, so the ISSUE-006 sweeper released it once that date aged past the window
# and the NAS update E2E failed for a reason unrelated to preservation.  Cover it
# here so the failure surfaces in unit CI instead of only in the slow E2E.
def test_ci_paid_state_fixture_survives_the_reservation_sweeper(app, tmp_path):
    import importlib.util
    import sqlite3

    database = app.extensions["inktime_database"]
    budgets = app.extensions["inktime_budget_service"]

    spec = importlib.util.spec_from_file_location(
        "persistence_fixture", "scripts/ci/persistence_fixture.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    connection = sqlite3.connect(database.path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        fixture.seed(connection)
        # Unknown paid state must survive scheduler reconciliation and updates.
        assert budgets.expire_stale_reservations() == 0
        fixture.verify(connection)
    finally:
        connection.close()


# ISSUE-007 -- a wrong (not merely missing) master secret was undetectable.
def test_master_secret_fingerprint_is_recorded(app):
    database = app.extensions["inktime_database"]
    with database.session() as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "runtime_identity" in tables


def test_master_secret_mismatch_is_refused(app, monkeypatch):
    from inktime.app.bootstrap import _assert_master_secret_identity

    database = app.extensions["inktime_database"]
    _assert_master_secret_identity(database, "first-secret", testing=False)
    # Same key -> fine.
    _assert_master_secret_identity(database, "first-secret", testing=False)
    # Different key -> refuse, because every device hash and stored secret was
    # derived from the original value.
    with pytest.raises(RuntimeError, match="SESSION-003"):
        _assert_master_secret_identity(database, "different-secret", testing=False)
    # Explicit opt-in re-stamps the identity instead of blocking startup.
    monkeypatch.setenv("INKTIME_ALLOW_SECRET_ROTATION", "1")
    _assert_master_secret_identity(database, "different-secret", testing=False)
    monkeypatch.delenv("INKTIME_ALLOW_SECRET_ROTATION")
    _assert_master_secret_identity(database, "different-secret", testing=False)


# ISSUE-008 -- retention policies shipped observation-only for ever.
def test_only_implemented_retention_policies_default_to_enforcement(app):
    database = app.extensions["inktime_database"]
    with database.session() as connection:
        rows = connection.execute(
            "SELECT data_type,dry_run FROM data_retention_policies WHERE data_type IN "
            "('decision_trace','decision_candidate','shadow_preview','device_event',"
            "'queue_event','job_log')"
        ).fetchall()
    assert rows, "expected the shipped operational retention policies to exist"
    observation_only = sorted(str(row["data_type"]) for row in rows if int(row["dry_run"]) == 1)
    assert observation_only == ["shadow_preview"]


def test_retention_deletes_expired_job_events_and_preserves_recent_ones(app):
    from inktime.app.repositories.resilience import ResilienceRepository

    database = app.extensions["inktime_database"]
    job_id = app.extensions["inktime_job_repository"].create_maintenance(
        kind="backup", name="retention regression", settings={}, created_by=None,
    )
    with database.transaction() as connection:
        for event, days in (("expired", 40), ("recent", 1)):
            connection.execute(
                "INSERT INTO job_events(job_id,event,message,created_at) VALUES (?,?,?,?)",
                (job_id, event, event, (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()),
            )
    ResilienceRepository(database).cleanup(dry_run=False)
    with database.session() as connection:
        events = {row[0] for row in connection.execute(
            "SELECT event FROM job_events WHERE job_id=?", (job_id,),
        )}
    assert "expired" not in events
    assert "recent" in events


@pytest.mark.parametrize("previous_version", [61, 62])
def test_upgrade_repairs_document_payloads_without_rewriting_prose(tmp_path, monkeypatch, previous_version):
    from inktime.app.db import Database
    from inktime.app.db import migrations
    from inktime.app.domain.analysis.schema import validate_analysis_result

    all_migrations = migrations.MIGRATIONS
    database = Database(tmp_path / "upgrade.db")
    monkeypatch.setattr(migrations, "MIGRATIONS", all_migrations[:previous_version])
    migrations.migrate(database)
    payload = _analysis_payload(types=["檔案"])
    raw = json.dumps(payload, ensure_ascii=True)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) "
            "VALUES ('l','test','/photos',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at) "
            "VALUES ('p','l','photo.jpg','analyzed',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO photo_analysis(photo_id,schema_version,stage,types_json,side_caption,raw_json,semantic_json,created_at) "
            "VALUES ('p',5,'single',?,?,?,?,datetime('now'))",
            (json.dumps(["檔案"]), payload["side_caption"], raw, raw),
        )
        connection.execute(
            "INSERT INTO ai_analysis_cache(content_sha256,provider,model_name,prompt_version,schema_version,"
            "schema_kind,result_json,raw_json,created_at) VALUES ('sha','p','m','v',5,'full',?,?,datetime('now'))",
            (raw, raw),
        )
        connection.execute(
            "INSERT INTO jobs(id,kind,name,status,strategy,settings_json,created_at) "
            "VALUES ('j','analysis','test','completed','single','{}',datetime('now'))"
        )
        connection.execute(
            "INSERT INTO job_items(id,job_id,result_json,available_at) VALUES ('i','j',?,datetime('now'))",
            (json.dumps({"analysis": payload, "message": "檔案"}, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT INTO ai_trace_runs(trace_id,photo_id,stage,status,started_at,final_result_json,created_at) "
            "VALUES ('t','p','single','SUCCESS',datetime('now'),?,datetime('now'))", (raw,),
        )
        connection.execute(
            "INSERT INTO ai_trace_attempts(trace_id,attempt_number,attempt_kind,provider,requested_model,status,"
            "response_raw_sanitized,response_parsed_json,created_at) "
            "VALUES ('t',1,'vision','p','m','SUCCESS',?,?,datetime('now'))", (raw, raw),
        )
        # Simulate the old PR's default promotion of the unsupported policy.
        connection.execute(
            "UPDATE data_retention_policies SET dry_run=0,updated_at=datetime('now') WHERE data_type='shadow_preview'"
        )
    monkeypatch.setattr(migrations, "MIGRATIONS", all_migrations)
    assert migrations.migrate(database) == list(range(previous_version + 1, 64))
    assert migrations.migrate(database) == []
    with database.session() as connection:
        row = connection.execute("SELECT * FROM photo_analysis WHERE photo_id='p'").fetchone()
        assert json.loads(row["types_json"]) == ["文件"]
        assert validate_analysis_result(row["raw_json"])["types"] == ["文件"]
        assert json.loads(row["semantic_json"])["types"] == ["文件"]
        assert row["side_caption"] == payload["side_caption"]
        for table, column in (
            ("photo_analysis", "raw_json"), ("ai_analysis_cache", "result_json"),
            ("ai_trace_runs", "final_result_json"), ("ai_trace_attempts", "response_parsed_json"),
        ):
            repaired = json.loads(connection.execute(
                f"SELECT {column} FROM {table}"  # noqa: S608 -- fixed test allowlist
            ).fetchone()[0])
            assert repaired["types"] == ["文件"]
            assert repaired["side_caption"] == payload["side_caption"]
        item = json.loads(connection.execute("SELECT result_json FROM job_items WHERE id='i'").fetchone()[0])
        assert item["analysis"]["types"] == ["文件"]
        assert item["message"] == "檔案"
        assert connection.execute("SELECT raw_json FROM ai_analysis_cache").fetchone()[0] == raw
        assert connection.execute("SELECT response_raw_sanitized FROM ai_trace_attempts").fetchone()[0] == raw
        assert connection.execute(
            "SELECT dry_run FROM data_retention_policies WHERE data_type='shadow_preview'"
        ).fetchone()[0] == 1


def test_shadow_upgrade_preserves_explicit_administrator_policy(tmp_path, monkeypatch):
    from inktime.app.db import Database
    from inktime.app.db import migrations

    all_migrations = migrations.MIGRATIONS
    database = Database(tmp_path / "custom-policy.db")
    monkeypatch.setattr(migrations, "MIGRATIONS", all_migrations[:62])
    migrations.migrate(database)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE data_retention_policies SET dry_run=0,updated_at=? WHERE data_type='shadow_preview'",
            (datetime.now(timezone.utc).isoformat(),),
        )
    monkeypatch.setattr(migrations, "MIGRATIONS", all_migrations)
    migrations.migrate(database)
    with database.session() as connection:
        assert connection.execute(
            "SELECT dry_run FROM data_retention_policies WHERE data_type='shadow_preview'"
        ).fetchone()[0] == 0


def test_document_repair_only_changes_protocol_types():
    from inktime.app.db.migrations import _repair_document_types

    value = {"types": ["檔案"], "side_caption": "檔案", "metadata": {"types": ["檔案"]}}
    assert _repair_document_types(value)
    assert value == {"types": ["文件"], "side_caption": "檔案", "metadata": {"types": ["檔案"]}}
    assert not _repair_document_types(value)
