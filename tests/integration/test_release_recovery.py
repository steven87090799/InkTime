from __future__ import annotations

import json
import sqlite3

import pytest
from PIL import Image


def _stage(publisher, profile: str):
    return publisher.publish(
        [("photo", Image.new("RGB", (480, 800), "white"))],
        profile_key=profile,
        activate=False,
    )


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
        coordinator.publish(
            [first, second], created_by="test", photo_ids=[], history=None
        )
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
    old = publisher.publish(
        [("old", Image.new("RGB", (480, 800), "black"))], profile_key="safe_4c"
    )
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
                another["release_id"], another["display_type"], another["width"],
                another["height"], another["pixel_format"], json.dumps(another),
                another["created_at"], "test", another["render_profile"],
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
