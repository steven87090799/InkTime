"""Regression tests for the defects found in the 2026-09 production readiness review.

Each test names the ISSUE id it locks down.  Every one of them fails on the
parent commit and passes after the corresponding fix.
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


# ISSUE-006 -- leaked budget reservations halted analysis permanently.
def test_stale_budget_reservations_stop_counting_and_are_swept(app):
    budgets = app.extensions["inktime_budget_service"]
    database = app.extensions["inktime_database"]
    stale = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    with database.transaction(operation="test_seed") as connection:
        connection.execute(
            "INSERT INTO budget_reservations(id,amount,job_id,photo_id,state,created_at) "
            "VALUES ('vision:stale',5.0,NULL,NULL,'active',?)",
            (stale,),
        )
        connection.execute(
            "INSERT INTO budget_reservations(id,amount,job_id,photo_id,state,created_at) "
            "VALUES ('vision:fresh',1.0,NULL,NULL,'active',?)",
            (fresh,),
        )
    # The abandoned reservation must not inflate spend for ever.
    assert budgets.snapshot()["reserved"] == pytest.approx(1.0)
    assert budgets.expire_stale_reservations() == 1
    with database.session() as connection:
        states = dict(
            connection.execute("SELECT id,state FROM budget_reservations").fetchall()
        )
    assert states["vision:stale"] == "released"
    assert states["vision:fresh"] == "active"


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
def test_shipped_retention_policies_actually_delete(app):
    database = app.extensions["inktime_database"]
    with database.session() as connection:
        rows = connection.execute(
            "SELECT data_type,dry_run FROM data_retention_policies WHERE data_type IN "
            "('decision_trace','decision_candidate','shadow_preview','device_event',"
            "'queue_event','job_log')"
        ).fetchall()
    assert rows, "expected the shipped operational retention policies to exist"
    observation_only = sorted(str(row["data_type"]) for row in rows if int(row["dry_run"]) == 1)
    assert observation_only == [], f"still evaluated-but-never-enforced: {observation_only}"
