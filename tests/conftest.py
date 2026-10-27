from __future__ import annotations

from pathlib import Path

import pytest
from inktime.app.core.runtime_config import RuntimeConfig
from inktime.app.factory import create_app


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "performance: scale and timing-sensitive validation reserved for performance lanes",
    )


@pytest.fixture
def app(tmp_path: Path):
    runtime_config = RuntimeConfig.from_sources(
        environ={},
        base_dir=tmp_path,
        environment="test",
        database_path=tmp_path / "inktime.db",
        data_dir=tmp_path / "data",
        release_dir=tmp_path / "releases",
        backup_dir=tmp_path / "backups",
        cache_dir=tmp_path / "cache",
        photo_dir=tmp_path / "photos",
        testing=True,
        development=False,
        cookie_secure=False,
    )
    application = create_app(runtime_config)
    yield application
    application.extensions["inktime_service_container"].close()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def custom_device_schedule(app):
    """Exercise explicit per-device times independently of the weekly default."""
    app.extensions["inktime_settings_repository"].update(
        "display.weekly_schedule_enabled", False, changed_by="test", source_ip="test"
    )


def csrf(client) -> str:
    with client.session_transaction() as session:
        return str(session.get("csrf_token", ""))


def create_admin(app, username: str = "admin", password: str = "very-safe-passphrase") -> str:
    return app.extensions["inktime_auth_repository"].create_user(username, password)


def login(client, username: str = "admin", password: str = "very-safe-passphrase"):
    client.get("/login")
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf(client)},
    )
    if response.status_code == 302:
        client.get("/dashboard")
    return response
