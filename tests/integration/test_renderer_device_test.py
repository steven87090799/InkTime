from __future__ import annotations

from io import BytesIO

from PIL import Image

from tests.conftest import create_admin, csrf, login


def _photo_upload(color: str = "#5079a8"):
    output = BytesIO()
    Image.new("RGB", (160, 240), color).save(output, "PNG")
    output.seek(0)
    return output, "person.png"


def test_browser_canvas_cannot_be_published_as_test_release(client, app):
    create_admin(app)
    login(client)
    device_id, _ = app.extensions["inktime_device_repository"].create(
        "六色測試", panel_profile="gdep073e01_6c"
    )
    response = client.post(
        "/api/v1/rendering/test-release",
        data={"device_id": device_id, "canvas_data": "data:image/png;base64,unsafe"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 400
    assert "Browser Canvas 不可直接發布" in response.get_json()["message"]


def test_ab_preview_is_server_rendered_and_reports_palette_statistics(client, app):
    create_admin(app)
    login(client)
    page = client.get("/simulator")
    assert page.status_code == 200
    assert "原圖" in page.get_data(as_text=True)
    assert "舊微雪算法" in page.get_data(as_text=True)
    assert "新算法" in page.get_data(as_text=True)
    response = client.post(
        "/api/v1/rendering/compare",
        data={
            "photo": _photo_upload("#c59d78"),
            "profile": "gdep073e01_6c",
            "preset": "photo_balanced",
            "fit": "cover",
            "options": '{"dither":"nearest"}',
            "palette": '{"mode":"default"}',
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["publish_source"] == "server_original_upload_only"
    assert body["payload_bytes"] == 192_000
    assert len(body["palette"]) == 6
    assert sum(item["pixels"] for item in body["palette"]) == 480 * 800


def test_test_release_is_one_time_and_does_not_overwrite_formal_schedule(client, app):
    create_admin(app)
    login(client)
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create(
        "六色測試",
        schedule="07:30",
        panel_profile="gdep073e01_6c",
    )
    publisher = app.extensions["inktime_release_publisher"]
    formal = publisher.publish(
        [("formal", Image.new("RGB", (480, 800), "white"))],
        profile_key="gdep073e01_6c",
    )
    pointer = app.config["INKTIME_RELEASE_DIR"] / "latest.gdep073e01_6c"
    response = client.post(
        "/api/v1/rendering/test-release",
        data={
            "photo": _photo_upload(),
            "device_id": device_id,
            "profile": "gdep073e01_6c",
            "preset": "photo_balanced",
            "fit": "cover",
            "options": '{"dither":"nearest"}',
            "palette": '{"mode":"default"}',
            "delivery": "next_wake",
            "one_time": "true",
            "restore_formal": "true",
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    test_release = response.get_json()
    assert test_release["formal_schedule_overwritten"] is False
    assert pointer.read_text() == formal["release_id"]
    assert repository.get(device_id)["schedule"] == "07:30"

    headers = {"Authorization": f"Bearer {token}"}
    assigned = client.get("/api/device/v1/releases/latest", headers=headers).get_json()
    assert assigned["release_id"] == test_release["release_id"]
    assert assigned["release_kind"] == "device_test"
    downloaded = client.get(
        assigned["download_base_url"] + assigned["files"][0]["name"], headers=headers
    )
    assert downloaded.status_code == 200
    downloaded.close()
    still_assigned = client.get("/api/device/v1/releases/latest", headers=headers).get_json()
    assert still_assigned["release_id"] == test_release["release_id"]
    acknowledged = client.post(
        "/api/device/v1/status",
        headers=headers,
        json={
            "release_id": test_release["release_id"],
            "payload_sha256_verified": True,
            "display_updated": True,
            "render_profile": "gdep073e01_6c",
            "error_code": "",
        },
    )
    assert acknowledged.status_code == 200
    restored = client.get("/api/device/v1/releases/latest", headers=headers).get_json()
    assert restored["release_id"] == formal["release_id"]
