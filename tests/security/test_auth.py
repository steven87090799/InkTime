from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import threading

import pytest

from inktime.app.domain.auth import AuthValidationError, SetupAlreadyCompleted
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


@pytest.mark.parametrize("password", ["", "1", "short-pass"])
def test_short_password_is_rejected(app, password):
    repository = app.extensions["inktime_auth_repository"]
    with pytest.raises(AuthValidationError, match="至少"):
        repository.create_user("short-password-user", password)


@pytest.mark.parametrize(
    ("username", "code"),
    [
        ("", "username_blank"),
        ("   ", "username_blank"),
        ("ab", "username_too_short"),
        ("a" * 65, "username_too_long"),
        ("bad\nname", "username_control_character"),
        ("管理員", "username_invalid_characters"),
    ],
)
def test_invalid_username_is_rejected(app, username, code):
    repository = app.extensions["inktime_auth_repository"]
    with pytest.raises(AuthValidationError) as raised:
        repository.create_user(username, "valid-password-long")
    assert raised.value.code == code


def test_username_uniqueness_is_case_insensitive(app):
    repository = app.extensions["inktime_auth_repository"]
    repository.create_user("Case.User", "valid-password-long")
    with pytest.raises(AuthValidationError) as raised:
        repository.create_user("case.user", "another-valid-password")
    assert raised.value.code == "username_taken"


def test_overlong_password_is_rejected_before_hashing(monkeypatch, app):
    called = False

    def unexpected_hash(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("hashing must not run")

    monkeypatch.setattr("inktime.app.core.security.generate_password_hash", unexpected_hash)
    with pytest.raises(AuthValidationError) as raised:
        app.extensions["inktime_auth_repository"].create_user("long-password", "x" * 129)
    assert raised.value.code == "password_too_long"
    assert called is False


def test_password_whitespace_is_preserved(app):
    repository = app.extensions["inktime_auth_repository"]
    password = "  passphrase with spaces  "
    repository.create_user("space-user", password)
    assert repository.authenticate("space-user", password) is not None
    assert repository.authenticate("space-user", password.strip()) is None


def test_password_is_never_written_to_logs(caplog, app):
    password = "secret-passphrase-never-log"
    with caplog.at_level(logging.DEBUG):
        app.extensions["inktime_auth_repository"].create_user("no-log-user", password)
    assert password not in caplog.text


def test_only_one_initial_administrator_can_be_created_concurrently(app):
    repository = app.extensions["inktime_auth_repository"]
    barrier = threading.Barrier(2)

    def create(username):
        barrier.wait(timeout=5)
        try:
            return ("created", repository.create_initial_administrator(username, "valid-password-long"))
        except SetupAlreadyCompleted:
            return ("already-completed", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, ("owner-one", "owner-two")))

    assert sorted(result[0] for result in results) == ["already-completed", "created"]
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


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


def test_viewer_cannot_update_device_energy_profile(client, app):
    device_id, _token = app.extensions["inktime_device_repository"].create("客廳")
    app.extensions["inktime_auth_repository"].create_user(
        "energy-viewer", "very-safe-viewer-password", "viewer"
    )
    login(client, "energy-viewer", "very-safe-viewer-password")

    response = client.patch(
        f"/api/v1/devices/{device_id}/energy-profile",
        json={"standby_current_ma": 0.12},
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


def test_disabled_user_existing_session_is_revoked(client, app):
    user_id = create_admin(app)
    login(client)
    app.extensions["inktime_auth_repository"].set_enabled(user_id, False)

    response = client.get("/dashboard")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    with client.session_transaction() as browser_session:
        assert "user_id" not in browser_session


def test_reenabled_user_must_login_again(client, app):
    user_id = create_admin(app)
    login(client)
    repository = app.extensions["inktime_auth_repository"]
    repository.set_enabled(user_id, False)
    repository.set_enabled(user_id, True)

    assert client.get("/dashboard").status_code == 302
    assert login(client).status_code == 302


def test_password_change_revokes_other_sessions(app):
    user_id = create_admin(app)
    first = app.test_client()
    second = app.test_client()
    login(first)
    login(second)
    app.extensions["inktime_auth_repository"].change_password(
        user_id,
        "very-safe-passphrase",
        "new-very-safe-passphrase",
    )

    assert first.get("/dashboard").status_code == 302
    assert second.get("/dashboard").status_code == 302


def test_role_change_revokes_existing_sessions(client, app):
    user_id = create_admin(app)
    login(client)
    app.extensions["inktime_auth_repository"].set_role(user_id, "viewer")

    assert client.get("/dashboard").status_code == 302


def test_invalid_session_version_is_rejected(client, app):
    user_id = create_admin(app)
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user_id
        browser_session["session_version"] = "1"

    assert client.get("/dashboard").status_code == 302
    with client.session_transaction() as browser_session:
        assert "user_id" not in browser_session


def test_create_user_api_uses_stable_validation_error(client, app):
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/users",
        json={"username": "new-user", "password": "short", "role": "viewer"},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "password_too_short",
            "message": "密碼至少需要 12 個字元。",
        }
    }
