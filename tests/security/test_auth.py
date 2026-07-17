from __future__ import annotations

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
    app.extensions["inktime_auth_repository"].create_user(
        "viewer", "very-safe-viewer-password", "viewer"
    )
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
