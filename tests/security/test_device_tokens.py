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
