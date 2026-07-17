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
    file_response = client.get(body["download_base_url"] + body["files"][0]["name"], headers=headers)
    assert file_response.status_code == 200
    assert len(file_response.data) == 96_000
    assert sha256(file_response.data).hexdigest() == body["files"][0]["sha256"]
