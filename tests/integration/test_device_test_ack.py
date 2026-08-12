from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PIL import Image

from inktime.app.domain.rendering import DeviceTestReleaseStore


def test_one_time_device_release_remains_retryable_until_verified_display_ack(client, app):
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create("ACK 測試", panel_profile="safe_4c")
    publisher = app.extensions["inktime_release_publisher"]
    formal = publisher.publish(
        [("formal", Image.new("RGB", (480, 800), "white"))],
        profile_key="safe_4c",
    )
    app.extensions["inktime_release_coordinator"].publish(
        [formal],
        created_by="integration-test",
        photo_ids=[],
    )
    test_release = publisher.publish(
        [("test", Image.new("RGB", (480, 800), "black"))],
        profile_key="safe_4c",
        activate=False,
    )
    store = DeviceTestReleaseStore(app.config["INKTIME_RELEASE_DIR"])
    store.assign(
        device_id,
        test_release["release_id"],
        profile_key="safe_4c",
        delivery="next_wake",
        one_time=True,
        restore_formal=True,
    )
    headers = {"Authorization": f"Bearer {token}"}

    manifest = client.get("/api/device/v1/releases/latest", headers=headers).get_json()
    response = client.get(manifest["download_base_url"] + manifest["files"][0]["name"], headers=headers)
    assert response.status_code == 200
    response.close()
    assert (
        client.get("/api/device/v1/releases/latest", headers=headers).get_json()["release_id"]
        == test_release["release_id"]
    )

    client.post(
        "/api/device/v1/status",
        headers=headers,
        json={
            "release_id": test_release["release_id"],
            "render_profile": "safe_4c",
            "payload_sha256_verified": False,
            "display_updated": True,
            "error_code": "",
        },
    )
    assert (
        client.get("/api/device/v1/releases/latest", headers=headers).get_json()["release_id"]
        == test_release["release_id"]
    )

    client.post(
        "/api/device/v1/status",
        headers=headers,
        json={
            "release_id": test_release["release_id"],
            "render_profile": "safe_4c",
            "payload_sha256_verified": True,
            "display_updated": True,
            "error_code": "",
        },
    )
    assert (
        client.get("/api/device/v1/releases/latest", headers=headers).get_json()["release_id"]
        == formal["release_id"]
    )


def test_device_status_ignores_delayed_older_reported_at(client, app):
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create("狀態單調性測試", panel_profile="safe_4c")
    headers = {"Authorization": f"Bearer {token}"}

    newer = client.post(
        "/api/device/v1/status",
        headers=headers,
        json={"status_reported_at": "2026-08-10T10:00:00Z", "error_message": "newer"},
    )
    older = client.post(
        "/api/device/v1/status",
        headers=headers,
        json={"status_reported_at": "2026-08-10T09:00:00Z", "error_message": "older"},
    )

    assert newer.status_code == 200
    assert newer.get_json() == {"status": "ok", "reason": None}
    assert older.status_code == 200
    assert older.get_json() == {"status": "ignored", "reason": "stale_status"}
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT last_status_at,last_error_message FROM devices WHERE id=?", (device_id,)
        ).fetchone()
    assert row["last_status_at"] == "2026-08-10T10:00:00+00:00"
    assert row["last_error_message"] == "newer"


def test_device_status_rejects_future_poisoning_and_clamps_small_future_skew(client, app):
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create("未來狀態防護", panel_profile="safe_4c")
    publisher = app.extensions["inktime_release_publisher"]
    formal = publisher.publish(
        [("formal", Image.new("RGB", (480, 800), "white"))],
        profile_key="safe_4c",
    )
    app.extensions["inktime_release_coordinator"].publish(
        [formal],
        created_by="integration-test",
        photo_ids=[],
    )
    test_release = publisher.publish(
        [("test", Image.new("RGB", (480, 800), "black"))],
        profile_key="safe_4c",
        activate=False,
    )
    DeviceTestReleaseStore(app.config["INKTIME_RELEASE_DIR"]).assign(
        device_id,
        test_release["release_id"],
        profile_key="safe_4c",
        delivery="next_wake",
        one_time=True,
        restore_formal=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    manifest = client.get("/api/device/v1/releases/latest", headers=headers).get_json()
    payload = client.get(manifest["download_base_url"] + manifest["files"][0]["name"], headers=headers)
    assert payload.status_code == 200
    payload.close()
    with app.extensions["inktime_database"].session() as connection:
        before = connection.execute(
            "SELECT last_status_at,last_error_message,acked_config_version,config_ack_at FROM devices WHERE id=?",
            (device_id,),
        ).fetchone()

    poisoned = client.post(
        "/api/device/v1/status",
        headers=headers,
        json={
            "status_reported_at": "2099-01-01T00:00:00Z",
            "release_id": test_release["release_id"],
            "render_profile": "safe_4c",
            "payload_sha256_verified": True,
            "display_updated": True,
            "applied_config_version": 1,
            "error_message": "future poison",
        },
    )
    assert poisoned.status_code == 200
    assert poisoned.get_json() == {"status": "ignored", "reason": "stale_status"}
    assert client.get("/api/device/v1/releases/latest", headers=headers).get_json()["release_id"] == test_release[
        "release_id"
    ]
    with app.extensions["inktime_database"].session() as connection:
        after_poison = connection.execute(
            "SELECT last_status_at,last_error_message,acked_config_version,config_ack_at FROM devices WHERE id=?",
            (device_id,),
        ).fetchone()
    assert tuple(after_poison) == tuple(before)

    recovered = client.post(
        "/api/device/v1/status",
        headers=headers,
        json={
            "release_id": test_release["release_id"],
            "render_profile": "safe_4c",
            "payload_sha256_verified": True,
            "display_updated": True,
            "applied_config_version": 1,
            "error_message": "normal recovery",
        },
    )
    assert recovered.status_code == 200
    assert recovered.get_json() == {"status": "ok", "reason": None}
    assert client.get("/api/device/v1/releases/latest", headers=headers).get_json()["release_id"] == formal[
        "release_id"
    ]

    small_future = datetime.now(timezone.utc) + timedelta(minutes=1)
    clamped = client.post(
        "/api/device/v1/status",
        headers=headers,
        json={"status_reported_at": small_future.isoformat(), "error_message": "small future"},
    )
    assert clamped.status_code == 200
    assert clamped.get_json() == {"status": "ok", "reason": None}
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT last_status_at,last_error_message FROM devices WHERE id=?", (device_id,)
        ).fetchone()
    assert row["last_error_message"] == "small future"
    assert datetime.fromisoformat(row["last_status_at"]) <= datetime.now(timezone.utc)
