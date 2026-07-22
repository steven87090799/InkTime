from __future__ import annotations

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
    response = client.get(
        manifest["download_base_url"] + manifest["files"][0]["name"], headers=headers
    )
    assert response.status_code == 200
    response.close()
    assert client.get("/api/device/v1/releases/latest", headers=headers).get_json()[
        "release_id"
    ] == test_release["release_id"]

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
    assert client.get("/api/device/v1/releases/latest", headers=headers).get_json()[
        "release_id"
    ] == test_release["release_id"]

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
    assert client.get("/api/device/v1/releases/latest", headers=headers).get_json()[
        "release_id"
    ] == formal["release_id"]
