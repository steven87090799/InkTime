from __future__ import annotations

from tests.conftest import create_admin, csrf, login


def test_provider_secret_is_encrypted_and_api_returns_only_mask(client, app):
    create_admin(app)
    login(client)
    api_key = "sk-sensitive-value-123456789"
    response = client.post(
        "/api/v1/providers",
        json={"name": "OpenAI", "base_url": "https://api.openai.com/v1", "api_key": api_key},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 201
    assert api_key.encode() not in app.extensions["inktime_database"].path.read_bytes()
    provider = app.extensions["inktime_provider_repository"].list()[0]
    assert provider["api_key_masked"].startswith("sk-s")
    assert api_key not in str(provider)


def test_setting_change_is_audited_without_manual_file_edit(client, app):
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/settings",
        json={"analysis.concurrency": 4, "general.timezone": "Asia/Taipei"},
        headers={
            "X-CSRF-Token": csrf(client),
            "X-InkTime-Confirm-Risk": "true",
        },
    )
    assert response.status_code == 200
    with app.extensions["inktime_database"].session() as connection:
        history = connection.execute("SELECT key,source_ip FROM setting_history ORDER BY id").fetchall()
    assert [row["key"] for row in history] == ["analysis.concurrency"]
    assert history[0]["source_ip"] == "127.0.0.1"


def test_viewer_cannot_change_settings(client, app):
    app.extensions["inktime_auth_repository"].create_user("viewer", "viewer-password-long", "viewer")
    login(client, "viewer", "viewer-password-long")
    response = client.post(
        "/api/v1/settings", json={"analysis.concurrency": 8}, headers={"X-CSRF-Token": csrf(client)}
    )
    assert response.status_code == 403
