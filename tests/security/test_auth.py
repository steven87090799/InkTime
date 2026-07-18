from __future__ import annotations

import pytest

from tests.conftest import create_admin, csrf, login


def test_first_start_setup_creates_administrator(client, app):
    response = client.get("/setup")
    assert response.status_code == 200
    response = client.post(
        "/setup",
        data={
            "username": "owner",
            "password": "long-owner-password",
            "password_confirmation": "long-owner-password",
            "csrf_token": csrf(client),
        },
    )
    assert response.status_code == 302
    with app.extensions["inktime_database"].session() as connection:
        user = connection.execute("SELECT username, role, password_hash FROM users").fetchone()
    assert user["username"] == "owner"
    assert user["role"] == "administrator"
    assert "long-owner-password" not in user["password_hash"]


def test_non_empty_short_password_is_allowed_but_empty_password_is_rejected(app):
    repository = app.extensions["inktime_auth_repository"]
    repository.create_user("short-password-user", "1")
    assert repository.authenticate("short-password-user", "1") is not None
    with pytest.raises(ValueError, match="不可空白"):
        repository.create_user("empty-password-user", "")


def test_stale_setup_csrf_token_refreshes_form_instead_of_showing_forbidden(client):
    client.get("/setup")
    with client.session_transaction() as browser_session:
        browser_session["csrf_token"] = "current-token"
    response = client.post(
        "/setup",
        data={
            "csrf_token": "stale-token",
            "username": "owner",
            "password": "1",
            "password_confirmation": "1",
        },
    )
    assert response.status_code == 303
    assert response.headers["Location"].endswith("/setup")
    refreshed = client.get(response.headers["Location"])
    assert "安全驗證已更新" in refreshed.get_data(as_text=True)


def test_unauthenticated_access_redirects_to_login(client, app):
    create_admin(app)
    response = client.get("/dashboard")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_csrf_is_required_for_mutation(client, app):
    create_admin(app)
    login(client)
    response = client.post("/api/v1/devices", json={"name": "客廳"})
    assert response.status_code == 403
    assert "AUTH-002" in response.get_data(as_text=True)


def test_viewer_cannot_create_device(client, app):
    app.extensions["inktime_auth_repository"].create_user("viewer", "very-safe-viewer-password", "viewer")
    login(client, "viewer", "very-safe-viewer-password")
    response = client.post(
        "/api/v1/devices",
        json={"name": "客廳"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 403


def test_login_failures_temporarily_block_ip(client, app):
    create_admin(app)
    client.get("/login")
    for _ in range(5):
        response = client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "csrf_token": csrf(client)},
        )
        assert response.status_code == 200
    response = client.post(
        "/login",
        data={"username": "admin", "password": "wrong", "csrf_token": csrf(client)},
    )
    assert response.status_code == 429
    assert "15 分鐘" in response.get_data(as_text=True)


def test_session_logout(client, app):
    create_admin(app)
    login(client)
    assert client.get("/dashboard").status_code == 200
    response = client.post("/logout", data={"csrf_token": csrf(client)})
    assert response.status_code == 302
    assert client.get("/dashboard").status_code == 302
