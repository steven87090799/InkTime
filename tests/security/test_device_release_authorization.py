from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
import shutil

from PIL import Image
import pytest

from inktime.app.core.paths import UnsafePathError
from inktime.app.repositories.resilience import ResilienceRepository
from inktime.app.services.device_releases import payload_entry_from_manifest


def _publish(app, name: str, *, activate: bool) -> dict:
    manifest = app.extensions["inktime_release_publisher"].publish(
        [(name, Image.new("RGB", (480, 800), "white"))],
        activate=activate,
    )
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            INSERT INTO releases(
                id,display_type,width,height,pixel_format,manifest_json,status,
                created_at,published_at,created_by,render_profile,verified_at,reconciliation_status
            ) VALUES (?,?,?,?,?,?,'published',?,?,?, ?,?,'ok')
            """,
            (
                manifest["release_id"],
                manifest["display_type"],
                manifest["width"],
                manifest["height"],
                manifest["pixel_format"],
                json.dumps(manifest),
                manifest["created_at"],
                datetime.now(timezone.utc).isoformat(),
                "security-test",
                manifest["render_profile"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return manifest


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _file_url(manifest: dict) -> str:
    return f"/api/device/v1/releases/{manifest['release_id']}/files/{manifest['files'][0]['name']}"


def _queue_release(app, device_id: str, release: dict) -> dict:
    repository = app.extensions["inktime_resilience_repository"]
    repository.ensure_queue(device_id)
    return repository.enqueue_release(device_id=device_id, release_id=release["release_id"])


def _queue_manifest(client, token: str) -> dict:
    response = client.get("/api/device/v1/queue/manifest", headers=_headers(token))
    assert response.status_code == 200
    return response.get_json()


def _rewrite_manifest(app, release: dict, **entry_changes) -> None:
    path = app.config["INKTIME_RELEASE_DIR"] / release["release_id"] / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0].update(entry_changes)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_device_release_authorization_sources_and_same_profile_isolation(client, app):
    repository = app.extensions["inktime_device_repository"]
    first_id, first_token = repository.create("first-device")
    second_id, _second_token = repository.create("second-device")
    first_release = _publish(app, "first-photo", activate=False)
    second_release = _publish(app, "second-photo", activate=False)
    profile_latest = _publish(app, "latest-photo", activate=True)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.executemany(
            """
            INSERT INTO device_render_releases(device_id,release_id,assigned_at)
            VALUES (?,?,?)
            """,
            (
                (
                    first_id,
                    first_release["release_id"],
                    datetime.now(timezone.utc).isoformat(),
                ),
                (
                    second_id,
                    second_release["release_id"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            ),
        )

    assert client.get(_file_url(first_release), headers=_headers(first_token)).status_code == 200
    assert client.get(_file_url(profile_latest), headers=_headers(first_token)).status_code == 200
    denied = client.get(_file_url(second_release), headers=_headers(first_token))
    assert denied.status_code == 404
    assert second_id not in denied.get_data(as_text=True)


def test_device_can_download_active_test_assignment_but_not_expired_assignment(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("test-device")
    release = _publish(app, "test-photo", activate=False)
    service = app.extensions["inktime_device_release_service"]
    service.test_store.assign(
        device_id,
        release["release_id"],
        profile_key="safe_4c",
        delivery="next_wake",
        one_time=True,
        restore_formal=True,
    )

    assert client.get(_file_url(release), headers=_headers(token)).status_code == 200

    assignment_path = app.config["INKTIME_RELEASE_DIR"] / ".device-tests" / f"{device_id}.json"
    assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
    assignment["expires_at"] = 0
    assignment_path.write_text(json.dumps(assignment), encoding="utf-8")
    assert client.get(_file_url(release), headers=_headers(token)).status_code == 404


def test_device_can_download_own_active_queue_release_but_not_cancelled_item(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("queue-device")
    release = _publish(app, "queue-photo", activate=False)
    repository = app.extensions["inktime_resilience_repository"]
    repository.ensure_queue(device_id)
    item = repository.enqueue_release(
        device_id=device_id,
        release_id=release["release_id"],
    )

    assert client.get(_file_url(release), headers=_headers(token)).status_code == 200

    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE device_content_queue_items SET status='CANCELLED' WHERE id=?",
            (item["id"],),
        )
    assert client.get(_file_url(release), headers=_headers(token)).status_code == 404


def test_unknown_release_and_path_traversal_are_not_disclosed(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("path-device")
    release = _publish(app, "path-photo", activate=False)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            INSERT INTO device_render_releases(device_id,release_id,assigned_at)
            VALUES (?,?,?)
            """,
            (device_id, release["release_id"], datetime.now(timezone.utc).isoformat()),
        )

    assert (
        client.get(
            "/api/device/v1/releases/unknown/files/photo.bin",
            headers=_headers(token),
        ).status_code
        == 404
    )
    response = client.get(
        f"/api/device/v1/releases/{release['release_id']}/files/%252e%252e%252fmanifest.json",
        headers=_headers(token),
    )
    assert response.status_code == 404


def test_profile_latest_requires_published_release_row(client, app):
    _, token = app.extensions["inktime_device_repository"].create("missing-row-device")
    app.extensions["inktime_release_publisher"].publish(
        [("missing-row-photo", Image.new("RGB", (480, 800), "white"))],
        activate=True,
    )

    response = client.get("/api/device/v1/releases/latest", headers=_headers(token))

    assert response.status_code == 404


def test_latest_release_quarantines_ambiguous_offline_slot_capability(client, app, monkeypatch):
    schedule_times = [f"{hour:02d}:00" for hour in range(13)]
    device_id, token = app.extensions["inktime_device_repository"].create(
        "ambiguous-offline-release",
        delivery_mode="inktime_offline_schedule",
        schedule_times=schedule_times,
        offline_schedule_max_slots=24,
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE devices
            SET offline_schedule_max_slots=12,
                offline_schedule_capability_state='legacy_ambiguous'
            WHERE id=?
            """,
            (device_id,),
        )
        row = connection.execute(
            "SELECT schedule_times_json FROM devices WHERE id=?",
            (device_id,),
        ).fetchone()
    assert json.loads(row["schedule_times_json"]) == schedule_times

    service = app.extensions["inktime_device_release_service"]
    monkeypatch.setattr(
        service,
        "latest_for_device",
        lambda **_kwargs: pytest.fail("ambiguous offline device must be quarantined first"),
    )

    response = client.get("/api/device/v1/releases/latest", headers=_headers(token))

    assert response.status_code == 409
    assert "DEVICE-008" in response.get_data(as_text=True)


def test_device_assignment_rejects_missing_release_row(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("stale-assignment")
    release = app.extensions["inktime_release_publisher"].publish(
        [("stale-photo", Image.new("RGB", (480, 800), "white"))],
        activate=False,
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO device_render_releases(device_id,release_id,assigned_at)
            VALUES (?,?,?)
            """,
            (device_id, release["release_id"], datetime.now(timezone.utc).isoformat()),
        )

    assert client.get(_file_url(release), headers=_headers(token)).status_code == 404


def test_queue_rejects_deleted_release_row(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("stale-queue")
    release = _publish(app, "queue-stale-photo", activate=False)
    repository = app.extensions["inktime_resilience_repository"]
    repository.ensure_queue(device_id)
    repository.enqueue_release(device_id=device_id, release_id=release["release_id"])
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM releases WHERE id=?", (release["release_id"],))

    assert client.get(_file_url(release), headers=_headers(token)).status_code == 404


def test_withdrawn_release_is_not_downloadable(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("withdrawn-device")
    release = _publish(app, "withdrawn-photo", activate=True)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE releases SET status='withdrawn' WHERE id=?",
            (release["release_id"],),
        )

    assert client.get(_file_url(release), headers=_headers(token)).status_code == 404


def test_test_assignment_uses_explicit_filesystem_release_policy(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("filesystem-test-device")
    release = app.extensions["inktime_release_publisher"].publish(
        [("filesystem-test-photo", Image.new("RGB", (480, 800), "white"))],
        activate=False,
    )
    app.extensions["inktime_device_release_service"].test_store.assign(
        device_id,
        release["release_id"],
        profile_key="safe_4c",
        delivery="next_wake",
        one_time=True,
        restore_formal=True,
    )

    assert client.get(_file_url(release), headers=_headers(token)).status_code == 200


def test_release_download_rejects_symlinked_intermediate_directory(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("symlink-directory")
    release = _publish(app, "symlink-directory-photo", activate=False)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            INSERT INTO device_render_releases(device_id,release_id,assigned_at)
            VALUES (?,?,?)
            """,
            (device_id, release["release_id"], datetime.now(timezone.utc).isoformat()),
        )
    release_path = app.config["INKTIME_RELEASE_DIR"] / release["release_id"]
    original_path = release_path.with_name(f"{release_path.name}.original")
    release_path.rename(original_path)
    release_path.symlink_to(original_path, target_is_directory=True)

    assert client.get(_file_url(release), headers=_headers(token)).status_code == 404


def test_release_directory_replacement_after_authorization_is_rejected(app):
    device_id, _token = app.extensions["inktime_device_repository"].create("replacement-device")
    release = _publish(app, "replacement-photo", activate=False)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            INSERT INTO device_render_releases(device_id,release_id,assigned_at)
            VALUES (?,?,?)
            """,
            (device_id, release["release_id"], datetime.now(timezone.utc).isoformat()),
        )
    service = app.extensions["inktime_device_release_service"]
    authorization = service.authorize_release_for_device(
        device_id=device_id,
        profile_key="safe_4c",
        release_id=release["release_id"],
    )
    release_path = app.config["INKTIME_RELEASE_DIR"] / release["release_id"]
    original_path = release_path.with_name(f"{release_path.name}.authorized")
    release_path.rename(original_path)
    shutil.copytree(original_path, release_path)

    with pytest.raises(UnsafePathError):
        service.read_payload(authorization, release["files"][0]["name"])


def test_release_file_is_hashed_and_returned_from_same_descriptor(app, monkeypatch):
    device_id, _token = app.extensions["inktime_device_repository"].create("descriptor-device")
    release = _publish(app, "descriptor-photo", activate=False)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            INSERT INTO device_render_releases(device_id,release_id,assigned_at)
            VALUES (?,?,?)
            """,
            (device_id, release["release_id"], datetime.now(timezone.utc).isoformat()),
        )
    service = app.extensions["inktime_device_release_service"]
    authorization = service.authorize_release_for_device(
        device_id=device_id,
        profile_key="safe_4c",
        release_id=release["release_id"],
    )
    filename = release["files"][0]["name"]
    payload_path = app.config["INKTIME_RELEASE_DIR"] / release["release_id"] / filename
    replaced_path = payload_path.with_suffix(".authorized")
    original_open = service._open_file_at

    def open_then_replace(directory_fd, selected_filename):
        handle = original_open(directory_fd, selected_filename)
        if selected_filename == filename:
            payload_path.rename(replaced_path)
            payload_path.write_bytes(b"replacement after descriptor open")
        return handle

    monkeypatch.setattr(service, "_open_file_at", open_then_replace)

    payload, entry = service.read_payload(authorization, filename)

    assert len(payload) == entry["size"]
    assert payload != payload_path.read_bytes()


def test_queue_manifest_uses_device_release_service(client, app, monkeypatch):
    device_id, token = app.extensions["inktime_device_repository"].create("manifest-service")
    release = _publish(app, "manifest-service-photo", activate=False)
    _queue_release(app, device_id, release)
    service = app.extensions["inktime_device_release_service"]
    original = service.authorize_release_for_device
    calls = []

    def tracked_authorization(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(service, "authorize_release_for_device", tracked_authorization)

    manifest = _queue_manifest(client, token)

    assert len(manifest["items"]) == 1
    assert calls == [
        {
            "device_id": device_id,
            "profile_key": "safe_4c",
            "release_id": release["release_id"],
        }
    ]


def test_queue_manifest_excludes_missing_release_row(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("missing-release-row")
    release = _publish(app, "missing-release-photo", activate=False)
    _queue_release(app, device_id, release)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM releases WHERE id=?", (release["release_id"],))

    assert _queue_manifest(client, token)["items"] == []


@pytest.mark.parametrize("status", ["withdrawn", "deleted", "staged", "staged_failed"])
def test_queue_manifest_excludes_nonpublished_release(client, app, status):
    device_id, token = app.extensions["inktime_device_repository"].create(f"release-{status}")
    release = _publish(app, f"release-{status}-photo", activate=False)
    _queue_release(app, device_id, release)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute("UPDATE releases SET status=? WHERE id=?", (status, release["release_id"]))

    assert _queue_manifest(client, token)["items"] == []


def test_queue_manifest_excludes_wrong_profile_release(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("profile-change")
    release = _publish(app, "profile-change-photo", activate=False)
    _queue_release(app, device_id, release)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute("UPDATE devices SET panel_profile='gdep073e01_6c' WHERE id=?", (device_id,))

    assert _queue_manifest(client, token)["items"] == []


def test_queue_manifest_excludes_other_devices_release(client, app):
    owner_id, _owner_token = app.extensions["inktime_device_repository"].create("queue-owner")
    _other_id, other_token = app.extensions["inktime_device_repository"].create("queue-other")
    release = _publish(app, "queue-owner-photo", activate=False)
    _queue_release(app, owner_id, release)

    assert _queue_manifest(client, other_token)["items"] == []


@pytest.mark.parametrize("status", ["CANCELLED", "EXPIRED", "FAILED", "PENDING"])
def test_queue_manifest_excludes_inactive_item(client, app, status):
    device_id, token = app.extensions["inktime_device_repository"].create(f"item-{status}")
    release = _publish(app, f"item-{status}-photo", activate=False)
    item = _queue_release(app, device_id, release)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE device_content_queue_items SET status=? WHERE id=?",
            (status, item["id"]),
        )

    assert _queue_manifest(client, token)["items"] == []


def test_queue_manifest_excludes_expired_item(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("expired-item")
    release = _publish(app, "expired-item-photo", activate=False)
    item = _queue_release(app, device_id, release)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE device_content_queue_items SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", item["id"]),
        )

    assert _queue_manifest(client, token)["items"] == []


def test_queue_manifest_rejects_symlinked_manifest(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("symlink-manifest")
    release = _publish(app, "symlink-manifest-photo", activate=False)
    _queue_release(app, device_id, release)
    path = app.config["INKTIME_RELEASE_DIR"] / release["release_id"] / "manifest.json"
    original = path.with_suffix(".original")
    path.rename(original)
    path.symlink_to(original.name)

    assert _queue_manifest(client, token)["items"] == []


def test_queue_manifest_rejects_replaced_release_directory(client, app, monkeypatch):
    device_id, token = app.extensions["inktime_device_repository"].create("replaced-queue-dir")
    release = _publish(app, "replaced-queue-photo", activate=False)
    _queue_release(app, device_id, release)
    service = app.extensions["inktime_device_release_service"]
    original_authorize = service.authorize_release_for_device
    release_path = app.config["INKTIME_RELEASE_DIR"] / release["release_id"]

    def replace_after_authorize(**kwargs):
        authorization = original_authorize(**kwargs)
        original_path = release_path.with_name(f"{release_path.name}.authorized")
        release_path.rename(original_path)
        shutil.copytree(original_path, release_path)
        return authorization

    monkeypatch.setattr(service, "authorize_release_for_device", replace_after_authorize)

    assert _queue_manifest(client, token)["items"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "../firmware.bin"),
        ("name", "sub/firmware.bin"),
        ("name", "firmware\\evil.bin"),
        ("name", "firmware\x00.bin"),
        ("name", "manifest.json"),
        ("size", True),
        ("size", "96000"),
        ("size", 0),
        ("sha256", "not-a-digest"),
    ],
)
def test_queue_manifest_rejects_invalid_payload_metadata(client, app, field, value):
    device_id, token = app.extensions["inktime_device_repository"].create(f"invalid-{field}")
    release = _publish(app, f"invalid-{field}-photo", activate=False)
    _queue_release(app, device_id, release)
    _rewrite_manifest(app, release, **{field: value})

    assert _queue_manifest(client, token)["items"] == []


def test_payload_entry_requires_exact_firmware_contract():
    valid = {"files": [{"name": "firmware 1.bin", "size": 10, "sha256": "A" * 64}]}

    assert payload_entry_from_manifest(valid) == {
        "name": "firmware 1.bin",
        "size": 10,
        "sha256": "a" * 64,
    }
    with pytest.raises(ValueError):
        payload_entry_from_manifest({"files": valid["files"] * 2})
    with pytest.raises(ValueError):
        payload_entry_from_manifest({"files": "firmware.bin"})


def test_queue_manifest_and_file_download_share_authorization_policy(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("shared-policy")
    release = _publish(app, "shared-policy-photo", activate=False)
    item = _queue_release(app, device_id, release)
    _rewrite_manifest(app, release, size=True)

    assert _queue_manifest(client, token)["items"] == []
    response = client.get(
        f"/api/device/v1/queue/items/{item['id']}/files/{release['files'][0]['name']}",
        headers=_headers(token),
    )
    assert response.status_code == 409


def test_queue_manifest_repository_has_no_release_filesystem_access():
    source = inspect.getsource(ResilienceRepository)

    assert "manifest_path" not in source
    assert "Path.read_text" not in source
    assert 'release_root / release_id / "manifest.json"' not in source
