from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from inktime.app.domain.rendering import release as release_module
from tests.conftest import create_admin, csrf, login
from tests.unit.test_analysis_schema import valid_result


def _stage(publisher, profile: str):
    return publisher.publish(
        [("photo", Image.new("RGB", (480, 800), "white"))],
        profile_key=profile,
        activate=False,
    )


def _gc_candidate(app, *, activate_pointers: bool = False, status: str = "superseded"):
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    manifest = _stage(publisher, "safe_4c")
    coordinator.publish(
        [manifest],
        created_by="test",
        photo_ids=[],
        activate_pointers=activate_pointers,
    )
    old = "2020-01-01T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE releases SET status=?,created_at=? WHERE id=?",
            (status, old, manifest["release_id"]),
        )
    return manifest["release_id"]


def _add_history_photo(app, tmp_path, release_id: str) -> str:
    root = tmp_path / f"history-{release_id}"
    root.mkdir()
    photo_id = f"history-{release_id[:12]}"
    Image.new("RGB", (32, 32), "white").save(root / "history.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("GC 歷史測試", root)
    now = "2026-08-01T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(
                id,library_id,relative_path,status,captured_at,captured_date,
                captured_month_day,capture_date_status,eligible,lifecycle_status,
                local_candidate_score,created_at,updated_at
            ) VALUES (?,?,'history.jpg','analyzed','2020-08-01T10:00:00','2020-08-01',
                      '08-01','valid',1,'active',80,?,?)
            """,
            (photo_id, library_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO display_history(
                photo_id,history_date,selection_method,release_id,displayed_at,metadata_json
            ) VALUES (?,'2020-08-01','gc-regression',?,?,'{}')
            """,
            (photo_id, release_id, now),
        )
    photos.save_analysis(
        photo_id,
        None,
        "local",
        "local",
        "gc-regression",
        valid_result(
            caption="這是一段 GC 歷史測試說明文字。",
            types=["日常"],
            memory_score=80,
            visual_score=80,
            side_caption="GC 歷史測試短句。",
        ),
        "{}",
        ranking_score=80,
        final_ranking_score=80,
    )
    return photo_id


def test_second_profile_activation_failure_restores_all_old_pointers(app, monkeypatch):
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    first = _stage(publisher, "safe_4c")
    second = _stage(publisher, "gdep073e01_6c")

    def fail_after_first(manifests):
        pointer = publisher.root / "latest.safe_4c"
        pointer.write_text(str(manifests[0]["release_id"]), encoding="utf-8")
        raise OSError("fault injection: second profile")

    monkeypatch.setattr(publisher, "activate_manifests", fail_after_first)
    with pytest.raises(OSError, match="second profile"):
        coordinator.publish([first, second], created_by="test", photo_ids=[], history=None)
    assert not (publisher.root / "latest.safe_4c").exists()
    with app.extensions["inktime_database"].session() as connection:
        statuses = {
            str(row["status"])
            for row in connection.execute(
                "SELECT status FROM releases WHERE id IN (?,?)",
                (first["release_id"], second["release_id"]),
            )
        }
    assert statuses == {"staged_failed"}


def test_display_history_failure_restores_pointer_and_recovery_marks_staged(app):
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    old = publisher.publish([("old", Image.new("RGB", (480, 800), "black"))], profile_key="safe_4c")
    staged = _stage(publisher, "safe_4c")
    with pytest.raises(sqlite3.IntegrityError):
        coordinator.publish(
            [staged],
            created_by="test",
            photo_ids=["missing-photo"],
            history={"history_date": "2026-07-22", "selection_method": "fault"},
        )
    assert (publisher.root / "latest.safe_4c").read_text() == old["release_id"]

    another = _stage(publisher, "gdep073e01_6c")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO releases(id,display_type,width,height,pixel_format,manifest_json,status,
                                 created_at,created_by,render_profile,verified_at)
            VALUES (?,?,?,?,?,?,'staged',?,?,?,datetime('now'))
            """,
            (
                another["release_id"],
                another["display_type"],
                another["width"],
                another["height"],
                another["pixel_format"],
                json.dumps(another),
                another["created_at"],
                "test",
                another["render_profile"],
            ),
        )
    assert coordinator.reconcile()["staged"] >= 1


def test_reconciliation_restores_missing_profile_pointer_to_latest_complete_release(app):
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    first = _stage(publisher, "safe_4c")
    coordinator.publish([first], created_by="test", photo_ids=[])
    second = _stage(publisher, "safe_4c")
    coordinator.publish([second], created_by="test", photo_ids=[])
    pointer = publisher.root / "latest.safe_4c"
    pointer.write_text("missing-release", encoding="utf-8")

    result = coordinator.reconcile()

    assert result["pointer_recovered"] == 1
    assert pointer.read_text(encoding="utf-8") == second["release_id"]


def test_gc_filesystem_quarantine_runs_without_sqlite_writer_lock(app, monkeypatch):
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    database = app.extensions["inktime_database"]
    release_id = _gc_candidate(app)
    observed = []

    def writer_lock_available():
        descriptor = os.open(database.writer_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return True
        finally:
            os.close(descriptor)

    original_fsync = release_module.fsync_directory

    def checked_slow_fsync(path):
        observed.append(writer_lock_available())
        time.sleep(0.02)
        return original_fsync(path)

    monkeypatch.setattr(release_module, "fsync_directory", checked_slow_fsync)
    result = coordinator.gc_unreferenced_releases(retention_days=1, max_items=1)

    assert result["deleted"] == 1
    assert observed == [True, True]
    assert not (publisher.root / release_id).exists()
    with database.session() as connection:
        state = connection.execute(
            "SELECT reconciliation_status FROM releases WHERE id=?", (release_id,)
        ).fetchone()
    assert state["reconciliation_status"] == "payload_pruned"


def test_gc_database_rollback_restores_quarantined_payload(app):
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    database = app.extensions["inktime_database"]
    release_id = _gc_candidate(app)
    with database.session() as connection:
        connection.execute(
            "CREATE TRIGGER gc_test_reject_update BEFORE UPDATE OF reconciliation_status ON releases "
            "WHEN NEW.reconciliation_status='payload_pruned' "
            "BEGIN SELECT RAISE(ABORT, 'gc rollback'); END"
        )
    try:
        result = coordinator.gc_unreferenced_releases(retention_days=1, max_items=1)
    finally:
        with database.session() as connection:
            connection.execute("DROP TRIGGER gc_test_reject_update")

    assert result == {"deleted": 0, "skipped": 1}
    assert (publisher.root / release_id).is_dir()
    with database.session() as connection:
        row = connection.execute(
            "SELECT reconciliation_status FROM releases WHERE id=?", (release_id,)
        ).fetchone()
    assert row["reconciliation_status"] == "ok"
    assert publisher.list_gc_quarantines() == []


def test_gc_purge_failure_leaves_orphan_for_next_maintenance(app, monkeypatch):
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    database = app.extensions["inktime_database"]
    release_id = _gc_candidate(app)
    with monkeypatch.context() as context:
        context.setattr(publisher, "purge_gc_quarantine", lambda _path: False)
        result = coordinator.gc_unreferenced_releases(retention_days=1, max_items=1)

    assert result == {"deleted": 1, "skipped": 1}
    assert (publisher.root / release_id).exists() is False
    with database.session() as connection:
        row = connection.execute(
            "SELECT reconciliation_status FROM releases WHERE id=?", (release_id,)
        ).fetchone()
    assert row["reconciliation_status"] == "payload_pruned"
    quarantines = publisher.list_gc_quarantines()
    assert len(quarantines) == 1
    assert quarantines[0][0] == release_id

    second = coordinator.gc_unreferenced_releases(retention_days=1, max_items=1)

    assert second["deleted"] == 0
    assert publisher.list_gc_quarantines() == []


def test_display_history_keeps_metadata_while_old_payload_is_pruned_and_gc_is_idempotent(
    app, tmp_path
):
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    database = app.extensions["inktime_database"]
    release_id = _gc_candidate(app)
    photo_id = _add_history_photo(app, tmp_path, release_id)

    first = coordinator.gc_unreferenced_releases(retention_days=1, max_items=1)
    second = coordinator.gc_unreferenced_releases(retention_days=1, max_items=1)

    assert first["deleted"] == 1
    assert second["deleted"] == 0
    assert not (publisher.root / release_id).exists()
    with database.session() as connection:
        release = connection.execute(
            "SELECT id,manifest_json,reconciliation_status FROM releases WHERE id=?", (release_id,)
        ).fetchone()
        history = connection.execute(
            "SELECT photo_id,release_id,selection_method FROM display_history WHERE release_id=?",
            (release_id,),
        ).fetchone()
    assert release["id"] == release_id
    assert json.loads(release["manifest_json"])["release_id"] == release_id
    assert release["reconciliation_status"] == "payload_pruned"
    assert tuple(history) == (photo_id, release_id, "gc-regression")
    reconciliation = coordinator.reconcile()
    assert reconciliation["payload_missing"] == 0
    assert not (publisher.root / release_id).exists()


@pytest.mark.parametrize(
    "reference_kind",
    [
        "current_pointer",
        "last_known_good",
        "fallback",
        "queue",
        "rollout",
        "device_assignment",
        "custom_assignment",
        "staged_publication",
    ],
)
def test_gc_preserves_every_active_release_reference(app, reference_kind):
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    database = app.extensions["inktime_database"]
    release_id = _gc_candidate(
        app,
        activate_pointers=reference_kind == "current_pointer",
        status="published",
    )
    devices = app.extensions["inktime_device_repository"]
    device_id, _token = devices.create(f"gc-{reference_kind}")

    if reference_kind in {"last_known_good", "fallback"}:
        app.extensions["inktime_resilience_repository"].ensure_queue(device_id)
        column = (
            "last_known_good_release_id"
            if reference_kind == "last_known_good"
            else "emergency_fallback_release_id"
        )
        with database.session() as connection:
            connection.execute(
                f"UPDATE device_content_queues SET {column}=? WHERE device_id=?",  # noqa: S608 - fixed test-only column set
                (release_id, device_id),
            )
    elif reference_kind == "queue":
        repository = app.extensions["inktime_resilience_repository"]
        repository.ensure_queue(device_id)
        repository.enqueue_release(device_id=device_id, release_id=release_id)
    elif reference_kind == "rollout":
        app.extensions["inktime_resilience_repository"].create_rollout(
            release_id=release_id,
            name="GC protection",
            user_id="test",
        )
    elif reference_kind == "device_assignment":
        with database.session() as connection:
            connection.execute(
                "INSERT INTO device_render_releases(device_id,release_id,assigned_at) VALUES (?,?,?)",
                (device_id, release_id, datetime.now(timezone.utc).isoformat()),
            )
    elif reference_kind == "custom_assignment":
        app.extensions["inktime_device_release_service"].test_store.assign(
            device_id,
            release_id,
            profile_key="safe_4c",
            delivery="next_wake",
            one_time=True,
            restore_formal=True,
        )
    elif reference_kind == "staged_publication":
        with database.session() as connection:
            connection.execute("UPDATE releases SET status='staged' WHERE id=?", (release_id,))

    result = coordinator.gc_unreferenced_releases(retention_days=1, max_items=1)

    assert result["deleted"] == 0
    assert (publisher.root / release_id).is_dir()
    with database.session() as connection:
        state = connection.execute(
            "SELECT reconciliation_status FROM releases WHERE id=?", (release_id,)
        ).fetchone()
    assert state["reconciliation_status"] == "ok"


def test_gc_preserves_committed_offline_schedule_payload(app):
    publisher = app.extensions["inktime_release_publisher"]
    coordinator = app.extensions["inktime_release_coordinator"]
    release_id = _gc_candidate(app, status="published")
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "gc-offline",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    app.extensions["inktime_offline_schedule_repository"].prepare_day(
        device_id=device_id,
        target_date=tomorrow,
        release_ids=[release_id],
    )

    result = coordinator.gc_unreferenced_releases(retention_days=1, max_items=1)

    assert result["deleted"] == 0
    assert (publisher.root / release_id).is_dir()


def test_pruned_payload_download_fails_closed_and_history_query_still_works(
    client, app, tmp_path
):
    release_id = _gc_candidate(app, status="published")
    _add_history_photo(app, tmp_path, release_id)
    coordinator = app.extensions["inktime_release_coordinator"]
    assert coordinator.gc_unreferenced_releases(retention_days=1, max_items=1)["deleted"] == 1

    device_id, token = app.extensions["inktime_device_repository"].create("gc-pruned-download")
    download = client.get(
        f"/api/device/v1/releases/{release_id}/files/photo_1.bin",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert download.status_code == 410
    assert download.json["error_code"] == "DEVICE-010"
    assert "Payload 已依保留政策移除" in download.json["message"]

    queue = app.extensions["inktime_resilience_repository"]
    queue.ensure_queue(device_id)
    with pytest.raises(ValueError, match="Release 不存在或不是已發布狀態"):
        queue.enqueue_release(device_id=device_id, release_id=release_id)
    with pytest.raises(ValueError, match="Release 不存在或不是已發布狀態"):
        queue.create_rollout(release_id=release_id, name="pruned", user_id="test")
    assert coordinator.reconcile()["payload_missing"] == 0

    create_admin(app)
    login(client)
    history = client.post(
        "/api/v1/rendering/history/select",
        json={},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert history.status_code == 200
    assert history.json["status"] == "ok"
