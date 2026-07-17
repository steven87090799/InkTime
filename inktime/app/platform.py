from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import os
import secrets

from flask import Flask, g, redirect, request, session, url_for
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.exceptions import HTTPException

from inktime import __version__
from inktime.app.api import auth, dashboard, devices, health, jobs
from inktime.app.db import Database, migrate
from inktime.app.repositories.auth import AuthRepository
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.repositories.jobs import JobRepository
from inktime.app.services.jobs import JobService
from inktime.app.web.access import csrf_token, verify_csrf


def _persistent_secret(path: Path) -> str:
    configured = os.environ.get("INKTIME_SECRET_KEY", "").strip()
    if configured:
        return configured
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(64)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return value


def initialize_platform(
    app: Flask,
    *,
    database_path: Path,
    data_dir: Path,
    release_dir: Path,
    testing: bool = False,
) -> Flask:
    data_dir.mkdir(parents=True, exist_ok=True)
    release_dir.mkdir(parents=True, exist_ok=True)
    database = Database(database_path)
    migrate(database, None if testing else data_dir / "backups")
    secret = "test-secret-not-for-production" if testing else _persistent_secret(data_dir / "session.key")

    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.environ.get("INKTIME_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        INKTIME_RELEASE_DIR=release_dir.resolve(),
        INKTIME_VERSION=__version__,
        TESTING=testing,
    )
    app.extensions["inktime_database"] = database
    app.extensions["inktime_auth_repository"] = AuthRepository(database)
    app.extensions["inktime_device_repository"] = DeviceRepository(database, secret)
    app.extensions["inktime_job_repository"] = JobRepository(database)
    app.extensions["inktime_job_service"] = JobService(app.extensions["inktime_job_repository"])

    web_root = Path(__file__).resolve().parent / "web"
    app.jinja_loader = ChoiceLoader(
        [app.jinja_loader, FileSystemLoader(str(web_root / "templates"))]
    )
    app.static_folder = str(web_root / "static")

    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(devices.bp)
    app.register_blueprint(health.bp)
    app.register_blueprint(jobs.bp)
    app.jinja_env.globals["csrf_token"] = csrf_token

    public_endpoints = {
        "auth.setup",
        "auth.login",
        "health.live",
        "health.ready",
        "devices.latest_release",
        "devices.release_file",
        "static",
    }

    @app.before_request
    def enforce_access():
        endpoint = request.endpoint or ""
        repository: AuthRepository = app.extensions["inktime_auth_repository"]
        user_id = session.get("user_id")
        g.user = repository.find_by_id(str(user_id)) if user_id else None

        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and endpoint not in {
            "devices.latest_release",
            "devices.release_file",
        }:
            verify_csrf()

        if endpoint in public_endpoints:
            return None
        if repository.count_users() == 0:
            return redirect(url_for("auth.setup"))
        if g.user is None:
            if request.path.startswith("/api/") or request.path.startswith("/health/detail"):
                return {"error_code": "AUTH-003", "message": "請先登入"}, 401
            return redirect(url_for("auth.login", next=request.full_path))
        return None

    @app.errorhandler(HTTPException)
    def stable_api_error(exc: HTTPException):
        if not request.path.startswith("/api/"):
            return exc
        description = str(exc.description)
        first, separator, remainder = description.partition(" ")
        error_code = first if "-" in first else "HTTP-{:03d}".format(exc.code or 500)
        message = remainder if separator else description
        return {"error_code": error_code, "message": message}, exc.code or 500

    return app
