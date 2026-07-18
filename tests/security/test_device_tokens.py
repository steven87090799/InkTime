from __future__ import annotations

from tests.conftest import create_admin, csrf, login


def test_device_token_is_returned_once_and_only_hash_is_stored(client, app, caplog):
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/devices",
        json={"name": "書房電子紙"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 201
    token = response.get_json()["token"]
    assert token.startswith("itd_")
    with app.extensions["inktime_database"].session() as connection:
        stored = connection.execute("SELECT token_hash FROM devices").fetchone()[0]
    assert token != stored
    assert token not in caplog.text


def test_device_can_be_fully_configured_when_created_from_web(client, app):
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/devices",
        json={
            "name": "臥室電子紙",
            "enabled": False,
            "timezone": "Asia/Tokyo",
            "schedule": "07:15",
            "rotation": 180,
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 201
    device = app.extensions["inktime_device_repository"].list()[0]
    assert device["name"] == "臥室電子紙"
    assert device["enabled"] == 0
    assert device["timezone"] == "Asia/Tokyo"
    assert device["schedule"] == "07:15"
    assert device["rotation"] == 180


def test_device_bearer_authentication_and_revocation(client, app):
    repository = app.extensions["inktime_device_repository"]
    device_id, old_token = repository.create("書房")
    response = client.get(
        "/api/device/v1/releases/latest",
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert response.status_code == 404
    repository.regenerate(device_id)
    response = client.get(
        "/api/device/v1/releases/latest",
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert response.status_code == 401


def test_device_token_is_not_accepted_in_url(client, app):
    _, token = app.extensions["inktime_device_repository"].create("書房")
    response = client.get(f"/api/device/v1/releases/latest?token={token}")
    assert response.status_code == 401


def test_device_downloads_versioned_manifest_and_verified_file(client, app):
    from PIL import Image
    from hashlib import sha256

    _, token = app.extensions["inktime_device_repository"].create("客廳")
    manifest = app.extensions["inktime_release_publisher"].publish(
        [("photo-1", Image.new("RGB", (480, 800), "white"))]
    )
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/device/v1/releases/latest", headers=headers)
    assert response.status_code == 200
    body = response.get_json()
    assert body["release_id"] == manifest["release_id"]
    assert body["pixel_format"] == "2bpp"
    assert body["device_config"] == {
        "schema_version": 2,
        "config_version": 1,
        "panel_profile": "safe_4c",
        "timezone": "Asia/Taipei",
        "utc_offset_minutes": 480,
        "schedule": "08:00",
        "rotation": 0,
    }
    file_response = client.get(body["download_base_url"] + body["files"][0]["name"], headers=headers)
    assert file_response.status_code == 200
    assert len(file_response.data) == 96_000
    assert sha256(file_response.data).hexdigest() == body["files"][0]["sha256"]


def test_administrator_can_disable_device_and_failed_download_is_counted(client, app):
    create_admin(app)
    login(client)
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create("測試裝置")
    response = client.get(
        "/api/device/v1/releases/missing/files/photo.bin",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404
    assert repository.list()[0]["download_failure_count"] == 1
    response = client.patch(
        f"/api/v1/devices/{device_id}",
        json={
            "name": "已停用裝置",
            "enabled": False,
            "timezone": "Asia/Taipei",
            "schedule": "08:30",
            "rotation": 0,
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    assert (
        client.get("/api/device/v1/releases/latest", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_device_status_is_recorded_without_exposing_token(client, app):
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create("客廳")
    response = client.post(
        "/api/device/v1/status",
        json={
            "firmware_version": "2.1.0",
            "wifi_rssi": -61,
            "free_heap_bytes": 182000,
            "free_psram_bytes": 7100000,
            "wake_reason": "4",
            "display_updated": False,
            "error_code": "DEVICE-DOWNLOAD",
            "error_message": "SHA-256 校驗失敗",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    device = repository.list()[0]
    assert device["id"] == device_id
    assert device["firmware_version"] == "2.1.0"
    assert device["wifi_rssi"] == -61
    assert device["last_error_code"] == "DEVICE-DOWNLOAD"
    assert repository.list_events()[0]["level"] == "error"


def test_device_status_rejects_malformed_numeric_telemetry(client, app):
    _, token = app.extensions["inktime_device_repository"].create("客廳")
    response = client.post(
        "/api/device/v1/status",
        json={"wifi_rssi": "not-a-number"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert "DEVICE-004" in response.get_data(as_text=True)


def test_device_configuration_version_is_acknowledged_only_after_report(client, app):
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create("七色電子紙", panel_profile="gdey073d46_7c")
    headers = {"Authorization": f"Bearer {token}"}

    with app.extensions["inktime_database"].session() as connection:
        before = connection.execute(
            "SELECT config_version,acked_config_version FROM devices WHERE id=?", (device_id,)
        ).fetchone()
    assert tuple(before) == (1, 0)

    repository.update(
        device_id,
        name="七色電子紙",
        enabled=True,
        timezone_name="Asia/Taipei",
        schedule="09:30",
        rotation=180,
        panel_profile="gdey073d46_7c",
    )
    with app.extensions["inktime_database"].session() as connection:
        desired = connection.execute(
            "SELECT config_version FROM devices WHERE id=?", (device_id,)
        ).fetchone()[0]
    assert desired == 2

    response = client.post(
        "/api/device/v1/status",
        json={"firmware_version": "2.2.0", "applied_config_version": desired},
        headers=headers,
    )
    assert response.status_code == 200
    device = repository.list()[0]
    assert device["acked_config_version"] == desired
    assert device["config_ack_at"] is not None


def test_device_receives_only_its_panel_profile_release(client, app):
    from PIL import Image

    _, token = app.extensions["inktime_device_repository"].create(
        "六色電子紙", panel_profile="gdep073e01_6c"
    )
    publisher = app.extensions["inktime_release_publisher"]
    publisher.publish(
        [("photo-six", Image.new("RGB", (480, 800), "blue"))],
        profile_key="gdep073e01_6c",
        dither="none",
    )
    publisher.publish(
        [("photo-seven", Image.new("RGB", (480, 800), "orange"))],
        profile_key="gdey073d46_7c",
        dither="none",
    )

    response = client.get(
        "/api/device/v1/releases/latest", headers={"Authorization": f"Bearer {token}"}
    )
    body = response.get_json()
    assert response.status_code == 200
    assert body["render_profile"] == "gdep073e01_6c"
    assert body["pixel_format"] == "indexed4"
    assert body["files"][0]["size"] == 192_000
