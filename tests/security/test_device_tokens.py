from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json

import pytest

from inktime.app.domain.rendering import DeviceTestReleaseStore
from inktime.app.repositories.devices import DeviceRateLimitError
from inktime.app.workers.scheduler import SchedulerRunner
from tests.conftest import create_admin, csrf, login


def _legacy_ambiguous_offline_device(app):
    schedule_times = [f"{hour:02d}:00" for hour in range(13)]
    device_id, token = app.extensions["inktime_device_repository"].create(
        "Legacy ambiguous remediation",
        delivery_mode="inktime_offline_schedule",
        schedule_times=schedule_times,
        offline_schedule_max_slots=24,
    )
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            UPDATE devices
            SET offline_schedule_max_slots=12,
                offline_schedule_capability_state='legacy_ambiguous',
                next_offline_prepare_at=NULL
            WHERE id=?
            """,
            (device_id,),
        )
    return device_id, token, schedule_times


def _legacy_ambiguous_raw_schedule_device(
    app,
    *,
    schedule_times_raw: str,
    offline_schedule_raw: str,
):
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create(
        "Malformed legacy remediation",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
        offline_schedule_max_slots=24,
    )
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            UPDATE devices
            SET offline_schedule_max_slots=12,
                offline_schedule_capability_state='legacy_ambiguous',
                schedule_times_json=?,offline_schedule_json=?,
                next_offline_prepare_at=NULL
            WHERE id=?
            """,
            (schedule_times_raw, offline_schedule_raw, device_id),
        )
    return device_id, token


def _device_form_payload(device, schedule_times, *, enabled: bool, delivery_mode: str) -> dict:
    return {
        "name": device["name"],
        "enabled": enabled,
        "timezone": device["timezone"],
        "schedule": device["schedule"],
        "delivery_mode": delivery_mode,
        "schedule_times": schedule_times,
        "offline_prefetch_allowed": delivery_mode == "inktime_offline_schedule",
        "minimum_schedule_gap_minutes": int(device["minimum_schedule_gap_minutes"]),
        "prefetch_lead_minutes": int(device["prefetch_lead_minutes"]),
        "sync_strategy": device["sync_strategy"],
        "sync_time": device["sync_time"],
        "button_wake_action": device["button_wake_action"],
        "stock_endpoint_host": device["stock_endpoint_host"],
        "rotation": int(device["rotation"]),
        "panel_profile": device["panel_profile"],
        "frame_orientation": device["frame_orientation"],
        "layout_mode": device["layout_mode"],
        "fit_mode": device["fit_mode"],
    }


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


@pytest.mark.parametrize("full_form", [False, True])
def test_administrator_can_disable_quarantined_device_without_rewriting_schedule(
    client,
    app,
    full_form,
):
    create_admin(app)
    login(client)
    device_id, _token, schedule_times = _legacy_ambiguous_offline_device(app)
    repository = app.extensions["inktime_device_repository"]
    before = repository.get(device_id)
    payload = (
        _device_form_payload(
            before,
            schedule_times,
            enabled=False,
            delivery_mode="inktime_offline_schedule",
        )
        if full_form
        else {"enabled": False}
    )

    response = client.patch(
        f"/api/v1/devices/{device_id}",
        json=payload,
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 200
    after = repository.get(device_id)
    assert after["enabled"] == 0
    assert after["offline_schedule_max_slots"] == 12
    assert after["offline_schedule_capability_state"] == "legacy_ambiguous"
    assert after["next_offline_prepare_at"] is None
    assert after["schedule_times_json"] == before["schedule_times_json"]
    assert after["offline_schedule_json"] == before["offline_schedule_json"]
    assert json.loads(str(after["schedule_times_json"])) == schedule_times


def test_full_form_can_switch_quarantined_device_away_from_offline_mode(client, app):
    create_admin(app)
    login(client)
    device_id, _token, schedule_times = _legacy_ambiguous_offline_device(app)
    repository = app.extensions["inktime_device_repository"]
    before = repository.get(device_id)
    payload = _device_form_payload(
        before,
        schedule_times,
        enabled=True,
        delivery_mode="legacy_online",
    )

    response = client.patch(
        f"/api/v1/devices/{device_id}",
        json=payload,
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 200
    after = repository.get(device_id)
    assert after["delivery_mode"] == "legacy_online"
    assert after["offline_prefetch_allowed"] == 0
    assert after["next_offline_prepare_at"] is None
    assert after["offline_schedule_max_slots"] == 12
    assert after["offline_schedule_capability_state"] == "legacy_ambiguous"
    assert after["schedule_times_json"] == before["schedule_times_json"]
    assert after["offline_schedule_json"] == before["offline_schedule_json"]
    assert json.loads(str(after["schedule_times_json"])) == schedule_times

    SchedulerRunner(app)._prepare_due_offline_devices(
        datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
    )
    assert not [
        job
        for job in app.extensions["inktime_job_repository"].list()
        if '"offline_prepare"' in str(job["settings_json"])
        and device_id in str(job["settings_json"])
    ]


def test_quarantined_device_rejects_explicit_oversized_schedule_mutation(client, app):
    create_admin(app)
    login(client)
    device_id, _token, schedule_times = _legacy_ambiguous_offline_device(app)
    repository = app.extensions["inktime_device_repository"]
    before = repository.get(device_id)
    changed_schedule = [f"{hour:02d}:00" for hour in range(1, 14)]

    response = client.patch(
        f"/api/v1/devices/{device_id}",
        json={"schedule_times": changed_schedule},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 400
    assert "1 到 12" in response.get_json()["message"]
    after = repository.get(device_id)
    assert after["offline_schedule_capability_state"] == "legacy_ambiguous"
    assert after["offline_schedule_max_slots"] == 12
    assert after["schedule_times_json"] == before["schedule_times_json"]
    assert json.loads(str(after["schedule_times_json"])) == schedule_times


@pytest.mark.parametrize(
    ("schedule_times_raw", "offline_schedule_raw"),
    [
        ('["08:00",', '["08:00"]'),
        ('{"legacy":"08:00"}', '"08:00"'),
    ],
)
@pytest.mark.parametrize("full_form", [False, True])
def test_administrator_can_disable_malformed_quarantine_without_normalizing_history(
    client,
    app,
    schedule_times_raw,
    offline_schedule_raw,
    full_form,
):
    create_admin(app)
    login(client)
    device_id, _token = _legacy_ambiguous_raw_schedule_device(
        app,
        schedule_times_raw=schedule_times_raw,
        offline_schedule_raw=offline_schedule_raw,
    )
    repository = app.extensions["inktime_device_repository"]
    before = repository.get(device_id)
    payload = (
        _device_form_payload(
            before,
            [str(before["schedule"])],
            enabled=False,
            delivery_mode="inktime_offline_schedule",
        )
        if full_form
        else {"enabled": False}
    )

    response = client.patch(
        f"/api/v1/devices/{device_id}",
        json=payload,
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 200
    after = repository.get(device_id)
    assert after["enabled"] == 0
    assert after["offline_schedule_capability_state"] == "legacy_ambiguous"
    assert after["offline_schedule_max_slots"] == 12
    assert after["next_offline_prepare_at"] is None
    assert after["schedule_times_json"] == schedule_times_raw
    assert after["offline_schedule_json"] == offline_schedule_raw
    assert after["offline_schedule_version"] == before["offline_schedule_version"]


def test_malformed_quarantine_mode_exit_is_exact_and_repeated_disable_has_no_version_churn(
    client,
    app,
):
    create_admin(app)
    login(client)
    schedule_times_raw = '["08:00",'
    offline_schedule_raw = '{"legacy":"08:00"}'
    device_id, _token = _legacy_ambiguous_raw_schedule_device(
        app,
        schedule_times_raw=schedule_times_raw,
        offline_schedule_raw=offline_schedule_raw,
    )
    repository = app.extensions["inktime_device_repository"]
    before = repository.get(device_id)
    switch_payload = _device_form_payload(
        before,
        [str(before["schedule"])],
        enabled=True,
        delivery_mode="legacy_online",
    )

    switched = client.patch(
        f"/api/v1/devices/{device_id}",
        json=switch_payload,
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert switched.status_code == 200
    after_switch = repository.get(device_id)
    assert after_switch["delivery_mode"] == "legacy_online"
    assert after_switch["offline_prefetch_allowed"] == 0
    assert after_switch["next_offline_prepare_at"] is None
    assert after_switch["offline_schedule_capability_state"] == "legacy_ambiguous"
    assert after_switch["schedule_times_json"] == schedule_times_raw
    assert after_switch["offline_schedule_json"] == offline_schedule_raw
    assert after_switch["offline_schedule_version"] == before["offline_schedule_version"]

    disable_payload = _device_form_payload(
        after_switch,
        [str(after_switch["schedule"])],
        enabled=False,
        delivery_mode="legacy_online",
    )
    first_disable = client.patch(
        f"/api/v1/devices/{device_id}",
        json=disable_payload,
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert first_disable.status_code == 200
    after_disable = repository.get(device_id)
    versions_after_disable = (
        int(after_disable["config_version"]),
        int(after_disable["offline_schedule_version"]),
    )

    repeated = client.patch(
        f"/api/v1/devices/{device_id}",
        json=disable_payload,
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert repeated.status_code == 200
    after_repeated = repository.get(device_id)
    assert (
        int(after_repeated["config_version"]),
        int(after_repeated["offline_schedule_version"]),
    ) == versions_after_disable
    assert after_repeated["schedule_times_json"] == schedule_times_raw
    assert after_repeated["offline_schedule_json"] == offline_schedule_raw


def test_malformed_quarantine_active_full_form_remains_fail_closed(client, app):
    create_admin(app)
    login(client)
    device_id, _token = _legacy_ambiguous_raw_schedule_device(
        app,
        schedule_times_raw='["08:00",',
        offline_schedule_raw='["08:00"]',
    )
    repository = app.extensions["inktime_device_repository"]
    before = repository.get(device_id)
    payload = _device_form_payload(
        before,
        [str(before["schedule"])],
        enabled=True,
        delivery_mode="inktime_offline_schedule",
    )

    response = client.patch(
        f"/api/v1/devices/{device_id}",
        json=payload,
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 409
    assert response.get_json()["error_code"] == "DEVICE-008"
    after = repository.get(device_id)
    assert after["schedule_times_json"] == before["schedule_times_json"]
    assert after["offline_schedule_json"] == before["offline_schedule_json"]
    assert after["config_version"] == before["config_version"]
    assert after["offline_schedule_version"] == before["offline_schedule_version"]


def test_malformed_quarantine_explicit_schedule_replacement_remains_capability_strict(
    client,
    app,
):
    create_admin(app)
    login(client)
    repository = app.extensions["inktime_device_repository"]
    rejected_id, _token = _legacy_ambiguous_raw_schedule_device(
        app,
        schedule_times_raw='["08:00",',
        offline_schedule_raw='{"legacy":"08:00"}',
    )
    oversized = [f"{hour:02d}:00" for hour in range(13)]

    rejected = client.patch(
        f"/api/v1/devices/{rejected_id}",
        json={"schedule_times": oversized},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert rejected.status_code == 400
    rejected_row = repository.get(rejected_id)
    assert rejected_row["schedule_times_json"] == '["08:00",'
    assert rejected_row["offline_schedule_json"] == '{"legacy":"08:00"}'
    assert rejected_row["offline_schedule_capability_state"] == "legacy_ambiguous"

    replaced_id, _token = _legacy_ambiguous_raw_schedule_device(
        app,
        schedule_times_raw='["08:00",',
        offline_schedule_raw='["08:00"]',
    )
    before_replacement = repository.get(replaced_id)
    replacement = ["08:00"]
    accepted = client.patch(
        f"/api/v1/devices/{replaced_id}",
        json={"schedule_times": replacement},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert accepted.status_code == 200
    accepted_row = repository.get(replaced_id)
    assert json.loads(str(accepted_row["schedule_times_json"])) == replacement
    assert json.loads(str(accepted_row["offline_schedule_json"])) == replacement
    assert accepted_row["offline_schedule_capability_state"] == "legacy_ambiguous"
    assert accepted_row["offline_schedule_max_slots"] == 12
    assert accepted_row["config_version"] == before_replacement["config_version"] + 1
    assert (
        accepted_row["offline_schedule_version"]
        == before_replacement["offline_schedule_version"] + 1
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
            "wake_reason_detail": "timer",
            "wifi_connect_ms": 1800,
            "wifi_fast_path_attempted": True,
            "wifi_fast_path_success": True,
            "network_session_ms": 9200,
            "http_request_count": 4,
            "tls_handshake_count_unavailable": True,
            "tls_handshake_count_unavailable_reason": (
                "transport_api_does_not_expose_handshake_count"
            ),
            "ntp_sync_attempted": False,
            "ntp_sync_succeeded": False,
            "ntp_sync_ms": 0,
            "download_bytes": 96000,
            "sd_read_bytes": 192000,
            "sd_write_bytes": 96000,
            "sd_write_ms": 240,
            "nvs_write_count": 3,
            "ack_event_count": 1,
            "ack_batch_request_count": 0,
            "i2c_retry_count": 2,
            "i2c_bus_reset_count": 1,
            "i2c_fail_closed_count": 0,
            "gc_deleted_files": 3,
            "gc_deleted_bytes": 720000,
            "gc_skipped_protected": 4,
            "epd_transfer_ms": 25000,
            "applied_offline_schedule_version": 7,
            "next_wake_epoch": 1760000000,
            "next_network_sync_epoch": 1759990000,
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
    assert details["wifi_connect_ms"] == 1800
    assert details["wifi_fast_path_success"] is True
    assert details["download_bytes"] == 96000
    assert details["nvs_write_count"] == 3
    assert details["i2c_retry_count"] == 2
    assert details["i2c_bus_reset_count"] == 1
    assert details["i2c_fail_closed_count"] == 0
    assert details["gc_deleted_files"] == 3
    assert details["gc_deleted_bytes"] == 720000
    assert details["gc_skipped_protected"] == 4
    assert details["tls_handshake_count_unavailable_reason"] == (
        "transport_api_does_not_expose_handshake_count"
    )
    assert details["next_network_sync_epoch"] == 1759990000
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
    "field,value",
    [
        ("wifi_connect_ms", 120_001),
        ("http_request_count", 129),
        ("download_bytes", 4_294_967_296),
        ("i2c_retry_count", 4_294_967_296),
        ("gc_deleted_bytes", 4_294_967_296),
        ("next_wake_epoch", -1),
        ("wifi_fast_path_success", "true"),
        ("wake_reason_detail", "x" * 65),
        ("tls_handshake_count_unavailable_reason", "x" * 65),
    ],
)
def test_device_status_rejects_unbounded_phase_two_telemetry(client, app, field, value):
    device_id, token = app.extensions["inktime_device_repository"].create("bounded-telemetry-device")
    response = client.post(
        "/api/device/v1/status",
        json={field: value},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM device_events WHERE device_id=?", (device_id,)
            ).fetchone()[0]
            == 0
        )


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


def test_offline_schedule_version_is_acknowledged_without_allowing_device_to_raise_desired(client, app):
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create(
        "離線排程 ACK",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
    )
    headers = {"Authorization": f"Bearer {token}"}
    desired = int(repository.get(device_id)["offline_schedule_version"])
    unknown = client.post(
        "/api/device/v1/status",
        json={
            "firmware_version": "2.8.0",
            "applied_offline_schedule_version": None,
        },
        headers=headers,
    )
    assert unknown.status_code == 200
    unknown_state = repository.get(device_id)
    assert unknown_state["applied_offline_schedule_version"] == 0
    assert unknown_state["offline_schedule_ack_at"] is None
    applied = client.post(
        "/api/device/v1/status",
        json={
            "firmware_version": "2.8.0",
            "applied_offline_schedule_version": desired,
        },
        headers=headers,
    )
    assert applied.status_code == 200
    acknowledged = repository.get(device_id)
    assert acknowledged["applied_offline_schedule_version"] == desired
    assert acknowledged["offline_schedule_ack_at"] is not None

    repository.update(
        device_id,
        name="離線排程 ACK",
        enabled=True,
        timezone_name="Asia/Taipei",
        schedule="09:00",
        schedule_times=["09:00", "20:00"],
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        rotation=0,
        panel_profile="safe_4c",
        prefetch_lead_minutes=5,
        button_wake_action="check_new",
    )
    desired_after_change = int(repository.get(device_id)["offline_schedule_version"])
    assert desired_after_change == desired + 1
    newer_than_desired = client.post(
        "/api/device/v1/status",
        json={
            "firmware_version": "2.8.0",
            "applied_offline_schedule_version": desired_after_change + 10,
        },
        headers=headers,
    )
    assert newer_than_desired.status_code == 200
    assert repository.get(device_id)["applied_offline_schedule_version"] == desired


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
