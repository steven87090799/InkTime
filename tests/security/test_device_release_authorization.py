from __future__ import annotations

from datetime import datetime, timezone
import json

from PIL import Image


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
