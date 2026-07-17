from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from inktime.app.platform import initialize_platform


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__)
    initialize_platform(
        application,
        database_path=tmp_path / "inktime.db",
        data_dir=tmp_path / "data",
        release_dir=tmp_path / "releases",
        testing=True,
    )
    return application


@pytest.fixture
def client(app):
    return app.test_client()


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
