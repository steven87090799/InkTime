from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from inktime.app.domain.rendering import DeviceTestReleaseStore
from inktime.app.repositories.devices import DeviceRateLimitError
from tests.conftest import create_admin, csrf, login


def test_web_cannot_precreate_a_custom_automatic_device(client, app):
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/devices",
        json={"name": "書房電子紙", "delivery_mode": "legacy_online"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 409
    assert app.extensions["inktime_device_repository"].list() == []


def test_web_created_device_is_stock_compatibility_only(client, app, caplog):
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/devices",
        json={"name": "Stock 書房相框", "delivery_mode": "stock_compat"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 201
    body = response.get_json()
    assert "token" not in body
    assert body["auth_mode"] == "stock"
    assert body["pairing_state"] == "paired"
    with app.extensions["inktime_database"].session() as connection:
        stored = connection.execute(
            "SELECT token_hash,auth_mode,pairing_state,device_secret_hash FROM devices"
        ).fetchone()
    assert stored["auth_mode"] == "stock"
    assert stored["pairing_state"] == "paired"
    assert stored["device_secret_hash"] is None
    assert stored["token_hash"]
    assert "Device Secret" not in caplog.text


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


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_device_enabled_rejects_non_boolean(client, app, value):
    create_admin(app)
    login(client)

    response = client.post(
        "/api/v1/devices",
        json={"name": "嚴格型別裝置", "enabled": value},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 400
    assert app.extensions["inktime_device_repository"].list() == []


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


def _trigger_device_auth_rate_limit(client) -> None:
    for index in range(20):
        response = client.get(
            "/api/device/v1/releases/latest",
            headers={"Authorization": f"Bearer invalid-{index}"},
        )
        assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "json_payload"),
    [
        ("get", "/api/device/v1/releases/latest", None),
        ("get", "/api/device/v1/queue/manifest", None),
        ("get", "/api/device/v1/queue/items/missing/files/photo.bin", None),
        ("post", "/api/device/v1/queue/ack", {}),
    ],
    ids=("release", "queue-manifest", "queue-file", "queue-ack"),
)
def test_device_auth_rate_limit_is_consistent(
    client,
    method,
    path,
    json_payload,
):
    _trigger_device_auth_rate_limit(client)

    response = client.open(
        path,
        method=method.upper(),
        json=json_payload,
        headers={"Authorization": "Bearer another-invalid-token"},
    )

    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 300
    assert response.get_json() == {
        "error_code": "DEVICE-007",
        "message": "裝置驗證嘗試過多，請稍後再試",
    }
    body = response.get_data(as_text=True)
    assert "another-invalid-token" not in body
    assert "attempt" not in body.casefold()


def test_valid_token_bypasses_shared_ip_failure_limit(client, app):
    _device_id, token = app.extensions["inktime_device_repository"].create("valid-behind-nat")
    _trigger_device_auth_rate_limit(client)

    response = client.post(
        "/api/device/v1/status",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200


def test_two_valid_devices_share_ip_without_lockout(client, app):
    _first_id, first = app.extensions["inktime_device_repository"].create("nat-first")
    _second_id, second = app.extensions["inktime_device_repository"].create("nat-second")
    _trigger_device_auth_rate_limit(client)

    for token in (first, second):
        response = client.post(
            "/api/device/v1/status",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200


def test_valid_token_does_not_clear_shared_ip_failures(client, app):
    _device_id, token = app.extensions["inktime_device_repository"].create("nat-valid")
    _trigger_device_auth_rate_limit(client)
    with app.extensions["inktime_database"].session() as connection:
        before = connection.execute("SELECT COUNT(*) FROM device_auth_failures").fetchone()[0]

    assert (
        client.post(
            "/api/device/v1/status",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
        == 200
    )
    with app.extensions["inktime_database"].session() as connection:
        after = connection.execute("SELECT COUNT(*) FROM device_auth_failures").fetchone()[0]

    assert before == after == 20


def test_invalid_token_remains_rate_limited_after_valid_device_auth(client, app):
    _device_id, token = app.extensions["inktime_device_repository"].create("nat-history")
    _trigger_device_auth_rate_limit(client)
    assert (
        client.post(
            "/api/device/v1/status",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
        == 200
    )

    response = client.get(
        "/api/device/v1/releases/latest",
        headers={"Authorization": "Bearer still-invalid"},
    )
    assert response.status_code == 429


@pytest.mark.parametrize("mode", ["disabled", "revoked"])
def test_disabled_or_revoked_device_token_does_not_bypass_rate_limit(client, app, mode):
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create(f"invalid-{mode}")
    if mode == "disabled":
        with app.extensions["inktime_database"].transaction() as connection:
            connection.execute("UPDATE devices SET enabled=0 WHERE id=?", (device_id,))
    else:
        repository.regenerate(device_id)
    _trigger_device_auth_rate_limit(client)

    response = client.get(
        "/api/device/v1/releases/latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 429


def test_concurrent_invalid_attempts_enforce_limit_atomically(app):
    repository = app.extensions["inktime_device_repository"]

    def attempt(index: int) -> str:
        try:
            result = repository.authenticate(f"concurrent-invalid-{index}", "203.0.113.10")
        except DeviceRateLimitError:
            return "limited"
        assert result is None
        return "invalid"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(40)))

    assert results.count("invalid") == 20
    assert results.count("limited") == 20
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM device_auth_failures").fetchone()[0] == 20


def test_rate_limit_storage_does_not_contain_raw_ip_or_token(app):
    repository = app.extensions["inktime_device_repository"]
    raw_ip = "198.51.100.42"
    raw_token = "plain-device-secret"

    assert repository.authenticate(raw_token, raw_ip) is None
    with app.extensions["inktime_database"].session() as connection:
        rows = connection.execute("SELECT ip_hash,attempted_at FROM device_auth_failures").fetchall()

    serialized = json.dumps([dict(row) for row in rows])
    assert raw_ip not in serialized
    assert raw_token not in serialized


@pytest.mark.parametrize(
    "path",
    [
        "/api/device/v1/releases/latest",
        "/api/device/v1/releases/missing/files/photo.bin",
        "/api/device/v1/status",
        "/api/device/v1/queue/manifest",
        "/api/device/v1/queue/items/missing/files/photo.bin",
        "/api/device/v1/queue/ack",
    ],
)
def test_device_auth_endpoints_share_identical_missing_token_behavior(client, path):
    response = client.open(
        path,
        method="POST" if path.endswith("/status") or path.endswith("/ack") else "GET",
        json={},
    )

    assert response.status_code == 401
    assert response.get_json() == {
        "error_code": "DEVICE-001",
        "message": "裝置驗證失敗",
    }


def test_device_downloads_versioned_manifest_and_verified_file(client, app):
    from PIL import Image
    from hashlib import sha256

    _, token = app.extensions["inktime_device_repository"].create("客廳")
    manifest = app.extensions["inktime_release_publisher"].publish(
        [("photo-1", Image.new("RGB", (480, 800), "white"))]
    )
    app.extensions["inktime_release_coordinator"].publish(
        [manifest],
        created_by="security-test",
        photo_ids=[],
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
            "board_profile": "waveshare-esp32-s3-photopainter",
            "flash_bytes": 16777216,
            "psram_bytes": 8388608,
            "flash_ready": True,
            "psram_ready": True,
            "sd_card": True,
            "rtc": True,
            "cache_status": "hit",
            "pmic_type": "axp2101",
            "usb_power": True,
            "battery_voltage": 4.08,
            "battery_percent": 82,
            "battery_percent_estimated": True,
            "temperature_c": 25.5,
            "humidity_percent": 61.0,
            "last_refresh_duration_ms": 25000,
            "wake_duration_ms": 61000,
            "wake_reason": "4",
            "display_updated": False,
            "display_skipped": True,
            "display_skip_reason": "same_sha256",
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
    event = repository.list_events()[0]
    assert event["level"] == "error"
    details = json.loads(event["details_json"])
    assert details["board_profile"] == "waveshare-esp32-s3-photopainter"
    assert details["pmic_type"] == "axp2101"
    assert details["cache_status"] == "hit"
    assert details["last_refresh_duration_ms"] == 25000
    assert details["display_skipped"] is True
    assert details["display_skip_reason"] == "same_sha256"
    with app.extensions["inktime_database"].session() as connection:
        sample = connection.execute(
            "SELECT * FROM device_power_samples WHERE device_id=?", (device_id,)
        ).fetchone()
    assert sample["battery_percent"] == 82
    assert sample["battery_percent_estimated"] == 1
    assert sample["refresh_duration_ms"] == 25000
    assert sample["wake_duration_ms"] == 61000
    assert sample["temperature_c"] == 25.5
    assert sample["humidity_percent"] == 61.0


def test_device_status_rejects_malformed_numeric_telemetry(client, app):
    _, token = app.extensions["inktime_device_repository"].create("客廳")
    response = client.post(
        "/api/device/v1/status",
        json={"wifi_rssi": "not-a-number"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert "DEVICE-004" in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"wifi_rssi": True},
        {"wifi_rssi": -61.5},
        {"wifi_rssi": "-61"},
        {"wifi_rssi": -128},
        {"battery_percent": 101},
        {"temperature_c": float("nan")},
        {"humidity_percent": float("inf")},
    ],
)
def test_device_status_rejects_ambiguous_or_out_of_range_numeric_values(
    client,
    app,
    payload,
):
    device_id, token = app.extensions["inktime_device_repository"].create("strict-number-device")

    response = client.post(
        "/api/device/v1/status",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM device_events WHERE device_id=?",
                (device_id,),
            ).fetchone()[0]
            == 0
        )


def test_device_status_rejects_json_array_without_writing_state(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("array-status-device")
    response = client.post(
        "/api/device/v1/status",
        json=[],
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM device_events WHERE device_id=?",
                (device_id,),
            ).fetchone()[0]
            == 0
        )


def test_device_status_rejects_oversized_body_before_parsing_or_writing(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("oversized-status-device")
    response = client.post(
        "/api/device/v1/status",
        data='{"error_message":"' + ("x" * (65 * 1024)) + '"}',
        content_type="application/json",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 413
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM device_events WHERE device_id=?",
                (device_id,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("value", ["true", "false", "1", "0", 1, 0, None, [], {}])
@pytest.mark.parametrize("field", ["display_updated", "payload_sha256_verified"])
def test_device_status_rejects_non_boolean_values(client, app, field, value):
    device_id, token = app.extensions["inktime_device_repository"].create("strict-boolean-device")
    response = client.post(
        "/api/device/v1/status",
        json={field: value},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM device_events WHERE device_id=?",
                (device_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM device_power_samples WHERE device_id=?",
                (device_id,),
            ).fetchone()[0]
            == 0
        )


def test_string_false_cannot_consume_assignment_or_complete_queue(client, app):
    from PIL import Image

    device_id, token = app.extensions["inktime_device_repository"].create("false-string-device")
    manifest = app.extensions["inktime_release_publisher"].publish(
        [("photo", Image.new("RGB", (480, 800), "white"))]
    )
    store = DeviceTestReleaseStore(app.config["INKTIME_RELEASE_DIR"])
    store.assign(
        device_id,
        manifest["release_id"],
        profile_key="safe_4c",
        delivery="next_wake",
        one_time=True,
        restore_formal=True,
    )
    assert store.active(device_id, "safe_4c") is not None
    store.mark_downloaded(device_id, manifest["release_id"])
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            INSERT INTO releases(
                id,display_type,width,height,pixel_format,manifest_json,status,
                created_at,published_at,render_profile,reconciliation_status
            ) VALUES (?,?,?,?,?,?,'published',datetime('now'),datetime('now'),?,'ok')
            """,
            (
                manifest["release_id"],
                manifest["display_type"],
                manifest["width"],
                manifest["height"],
                manifest["pixel_format"],
                json.dumps(manifest),
                manifest["render_profile"],
            ),
        )
    queue = app.extensions["inktime_resilience_repository"]
    queue.ensure_queue(device_id)
    item = queue.enqueue_release(device_id=device_id, release_id=manifest["release_id"])

    response = client.post(
        "/api/device/v1/status",
        json={
            "release_id": manifest["release_id"],
            "payload_sha256_verified": "false",
            "display_updated": "false",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assignment = store.active(device_id, "safe_4c")
    assert assignment is not None
    assert assignment["status"] == "payload_downloaded"
    with app.extensions["inktime_database"].session() as connection:
        status = connection.execute(
            "SELECT status FROM device_content_queue_items WHERE id=?",
            (item["id"],),
        ).fetchone()[0]
    assert status == "READY"


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

    _, token = app.extensions["inktime_device_repository"].create("六色電子紙", panel_profile="gdep073e01_6c")
    publisher = app.extensions["inktime_release_publisher"]
    six_color = publisher.publish(
        [("photo-six", Image.new("RGB", (480, 800), "blue"))],
        profile_key="gdep073e01_6c",
        dither="none",
    )
    seven_color = publisher.publish(
        [("photo-seven", Image.new("RGB", (480, 800), "orange"))],
        profile_key="gdey073d46_7c",
        dither="none",
    )
    app.extensions["inktime_release_coordinator"].publish(
        [six_color, seven_color],
        created_by="security-test",
        photo_ids=[],
    )

    response = client.get("/api/device/v1/releases/latest", headers={"Authorization": f"Bearer {token}"})
    body = response.get_json()
    assert response.status_code == 200
    assert body["render_profile"] == "gdep073e01_6c"
    assert body["pixel_format"] == "indexed4"
    assert body["files"][0]["size"] == 192_000
