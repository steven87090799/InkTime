from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import threading

import pytest

from inktime.app.core.security import hash_password
from inktime.app.domain.auth import (
    AuthValidationError,
    LastAdministratorRequired,
    SetupAlreadyCompleted,
)
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


def test_migrated_unicode_username_keeps_exact_login_compatibility(app):
    password = "legacy-unicode-password"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO users(
                id,username,normalized_username,password_hash,role,enabled,
                session_version,password_changed_at,created_at
            ) VALUES ('legacy-user','Straße','straße',?,'administrator',1,1,datetime('now'),datetime('now'))
            """,
            (hash_password(password),),
        )

    assert app.extensions["inktime_auth_repository"].authenticate("Straße", password) is not None


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
    app.extensions["inktime_auth_repository"].create_user(
        "backup-admin",
        "very-safe-backup-passphrase",
        "administrator",
    )
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
    repository.create_user(
        "backup-admin",
        "very-safe-backup-passphrase",
        "administrator",
    )
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
    app.extensions["inktime_auth_repository"].create_user(
        "backup-admin",
        "very-safe-backup-passphrase",
        "administrator",
    )
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
    assert response.get_json()["error"] == {
        "code": "password_too_short",
        "message": "密碼至少需要 12 個字元。",
    }
    assert "至少需要 12 個字元" in response.json["user_error"]["detail"]


def _user_state(app, user_id: str):
    with app.extensions["inktime_database"].session() as connection:
        return connection.execute(
            "SELECT enabled,role,session_version FROM users WHERE id=?",
            (user_id,),
        ).fetchone()


def test_cannot_disable_last_enabled_administrator(app):
    user_id = create_admin(app)
    repository = app.extensions["inktime_auth_repository"]

    with pytest.raises(LastAdministratorRequired) as raised:
        repository.update_user_security_state(user_id=user_id, enabled=False)

    assert raised.value.code == "last_administrator_required"
    assert tuple(_user_state(app, user_id)) == (1, "administrator", 1)


def test_cannot_demote_last_enabled_administrator(app):
    user_id = create_admin(app)
    repository = app.extensions["inktime_auth_repository"]

    with pytest.raises(LastAdministratorRequired):
        repository.update_user_security_state(user_id=user_id, role="viewer")

    assert tuple(_user_state(app, user_id)) == (1, "administrator", 1)


def test_cannot_disable_and_demote_last_administrator(app):
    user_id = create_admin(app)
    repository = app.extensions["inktime_auth_repository"]

    with pytest.raises(LastAdministratorRequired):
        repository.update_user_security_state(
            user_id=user_id,
            enabled=False,
            role="viewer",
        )

    assert tuple(_user_state(app, user_id)) == (1, "administrator", 1)


def test_can_disable_administrator_when_another_enabled_admin_exists(app):
    repository = app.extensions["inktime_auth_repository"]
    first_id = create_admin(app)
    repository.create_user("second-admin", "very-safe-second-pass", "administrator")

    updated = repository.update_user_security_state(user_id=first_id, enabled=False)

    assert (updated["enabled"], updated["role"], updated["session_version"]) == (
        0,
        "administrator",
        2,
    )


def test_can_demote_administrator_when_another_enabled_admin_exists(app):
    repository = app.extensions["inktime_auth_repository"]
    first_id = create_admin(app)
    repository.create_user("second-admin", "very-safe-second-pass", "administrator")

    updated = repository.update_user_security_state(user_id=first_id, role="viewer")

    assert (updated["enabled"], updated["role"], updated["session_version"]) == (1, "viewer", 2)


@pytest.mark.parametrize(
    ("first_changes", "second_changes"),
    [
        ({"enabled": False}, {"enabled": False}),
        ({"role": "viewer"}, {"role": "viewer"}),
        ({"enabled": False}, {"role": "viewer"}),
    ],
)
def test_concurrent_admin_updates_cannot_leave_zero_administrators(
    app,
    first_changes,
    second_changes,
):
    repository = app.extensions["inktime_auth_repository"]
    first_id = create_admin(app)
    second_id = repository.create_user(
        "second-admin",
        "very-safe-second-pass",
        "administrator",
    )
    barrier = threading.Barrier(2)

    def update(item: tuple[str, dict]) -> str:
        user_id, changes = item
        barrier.wait(timeout=5)
        try:
            repository.update_user_security_state(user_id=user_id, **changes)
        except LastAdministratorRequired:
            return "rejected"
        return "updated"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                update,
                ((first_id, first_changes), (second_id, second_changes)),
            )
        )

    assert sorted(results) == ["rejected", "updated"]
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM users WHERE enabled=1 AND role='administrator'"
            ).fetchone()[0]
            == 1
        )


def test_rejected_last_admin_update_does_not_change_session_version(app):
    user_id = create_admin(app)

    with pytest.raises(LastAdministratorRequired):
        app.extensions["inktime_auth_repository"].update_user_security_state(
            user_id=user_id,
            enabled=False,
        )

    assert _user_state(app, user_id)["session_version"] == 1


def test_last_admin_api_rejection_uses_stable_conflict_error(client, app):
    user_id = create_admin(app)
    login(client)

    response = client.patch(
        f"/api/v1/users/{user_id}",
        json={"enabled": False},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == {
        "code": "last_administrator_required",
        "message": "系統至少必須保留一位啟用中的管理員。",
    }
    assert "管理員" in response.json["user_error"]["title"]
    assert tuple(_user_state(app, user_id)) == (1, "administrator", 1)


def test_user_patch_is_atomic_when_role_is_invalid(client, app):
    user_id = create_admin(app)
    login(client)

    response = client.patch(
        f"/api/v1/users/{user_id}",
        json={"enabled": False, "role": "invalid-role"},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 400
    assert tuple(_user_state(app, user_id)) == (1, "administrator", 1)


def test_user_patch_is_atomic_when_enabled_type_is_invalid(client, app):
    user_id = create_admin(app)
    login(client)

    response = client.patch(
        f"/api/v1/users/{user_id}",
        json={"enabled": "false", "role": "viewer"},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 400
    assert tuple(_user_state(app, user_id)) == (1, "administrator", 1)


def test_combined_role_and_enabled_update_commits_together(client, app):
    repository = app.extensions["inktime_auth_repository"]
    first_id = create_admin(app)
    repository.create_user("second-admin", "very-safe-second-pass", "administrator")
    login(client)

    response = client.patch(
        f"/api/v1/users/{first_id}",
        json={"enabled": False, "role": "viewer"},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 200
    assert tuple(_user_state(app, first_id)) == (0, "viewer", 2)


def test_combined_update_increments_session_version_once(client, app):
    repository = app.extensions["inktime_auth_repository"]
    first_id = create_admin(app)
    repository.create_user("second-admin", "very-safe-second-pass", "administrator")
    login(client)

    response = client.patch(
        f"/api/v1/users/{first_id}",
        json={"enabled": False, "role": "viewer"},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 200
    assert response.get_json()["session_version"] == 2


def test_noop_user_patch_does_not_increment_session_version(client, app):
    user_id = create_admin(app)
    login(client)

    response = client.patch(
        f"/api/v1/users/{user_id}",
        json={"enabled": True, "role": "administrator"},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 200
    assert response.get_json()["session_version"] == 1
    assert client.get("/dashboard").status_code == 200


def test_unknown_user_patch_fields_are_rejected(client, app):
    user_id = create_admin(app)
    login(client)

    response = client.patch(
        f"/api/v1/users/{user_id}",
        json={"enabled": True, "unexpected": False},
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 400
    assert tuple(_user_state(app, user_id)) == (1, "administrator", 1)
