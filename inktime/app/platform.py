from __future__ import annotations

from datetime import timedelta
import logging
from pathlib import Path
import secrets

from flask import Flask, flash, g, jsonify, redirect, request, session, url_for
from jinja2 import FileSystemLoader
from werkzeug.exceptions import HTTPException

from inktime import __version__
from inktime.app.api import (
    auth,
    batches,
    dashboard,
    devices,
    health,
    jobs,
    notifications,
    operations,
    photos,
    resilience,
    review,
    rendering,
    scoring,
    settings,
)
from inktime.app.bootstrap import ServiceContainer, bootstrap_services
from inktime.app.core.logging import log_event
from inktime.app.core.errors import ApplicationError
from inktime.app.core.redirects import safe_local_redirect_target
from inktime.app.core.runtime_config import RuntimeConfig
from inktime.app.repositories.auth import AuthRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.web.access import csrf_token, verify_csrf


LOGGER = logging.getLogger("platform")


def configure_web_application(
    app: Flask,
    runtime_config: RuntimeConfig,
    container: ServiceContainer,
) -> Flask:
    """Attach one already-bootstrapped service graph and the modern Web surface."""

    if app.extensions.get("inktime_platform_initialized"):
        raise RuntimeError("initialize_platform() 不得對同一 App 重複執行")
    app.extensions.update(container.extensions)
    app.extensions["inktime_service_container"] = container
    app.extensions["inktime_platform_initialized"] = True
    app.secret_key = container.session_secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=runtime_config.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        INKTIME_RELEASE_DIR=runtime_config.release_dir,
        INKTIME_RENDER_WORK_DIR=runtime_config.cache_dir / "render-workloads",
        INKTIME_PHOTO_DIR=runtime_config.photo_dir,
        INKTIME_VERSION=__version__,
        INKTIME_ENABLE_LEGACY_WEBUI=runtime_config.legacy_enabled,
        PREFERRED_URL_SCHEME=runtime_config.public_url.split(":", 1)[0],
        TESTING=runtime_config.testing,
    )
    settings_repository: SettingsRepository = container.extensions["inktime_settings_repository"]  # type: ignore[assignment]
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=int(settings_repository.get("security.session_minutes", 30))
    )

    web_root = Path(__file__).resolve().parent / "web"
    app.jinja_loader = FileSystemLoader(str(web_root / "templates"))
    app.static_folder = str(web_root / "static")
    app.static_url_path = "/static"

    for blueprint in (
        auth.bp,
        batches.bp,
        dashboard.bp,
        devices.bp,
        health.bp,
        jobs.bp,
        notifications.bp,
        photos.bp,
        settings.bp,
        scoring.bp,
        operations.bp,
        rendering.bp,
        resilience.bp,
        review.bp,
    ):
        app.register_blueprint(blueprint)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.get("/")
    def modern_root():
        return redirect(url_for("dashboard.dashboard"))

    @app.context_processor
    def critical_alerts():
        database = container.extensions["inktime_database"]
        try:
            with database.session() as connection:
                rows = connection.execute(
                    "SELECT component,error_code,message,last_seen_at FROM job_errors "
                    "WHERE resolved_at IS NULL AND lower(severity)='critical' "
                    "ORDER BY last_seen_at DESC LIMIT 3"
                ).fetchall()
        except Exception:
            rows = []
        return {
            "critical_alerts": rows,
            "csp_nonce": getattr(g, "csp_nonce", ""),
        }

    public_endpoints = {
        "auth.setup",
        "auth.login",
        "health.live",
        "health.ready",
        "devices.latest_release",
        "devices.stock_data_up_payload",
        "devices.device_offline_schedule",
        "devices.release_file",
        "devices.report_status",
        "resilience.device_queue_manifest",
        "resilience.device_queue_ack",
        "resilience.queue_item_file",
        "static",
    }
    device_endpoints = {
        "devices.latest_release",
        "devices.stock_data_up_payload",
        "devices.device_offline_schedule",
        "devices.release_file",
        "devices.report_status",
        "resilience.device_queue_ack",
    }

    @app.before_request
    def enforce_access():
        g.csp_nonce = secrets.token_urlsafe(24)
        endpoint = request.endpoint or ""
        repository: AuthRepository = app.extensions["inktime_auth_repository"]
        user_id = session.get("user_id")
        g.user = (
            repository.find_session_user(str(user_id), session.get("session_version")) if user_id else None
        )
        if user_id and g.user is None:
            session.clear()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and endpoint not in device_endpoints:
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

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            f"script-src 'self' 'nonce-{g.csp_nonce}'; connect-src 'self'",
        )
        if (
            runtime_config.environment == "production"
            and runtime_config.public_url.startswith("https://")
            and request.is_secure
        ):
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response

    @app.errorhandler(HTTPException)
    def stable_error(exc: HTTPException):
        if (
            exc.code == 403
            and str(exc.description).startswith("AUTH-002")
            and request.path in {"/setup", "/login"}
        ):
            session.pop("csrf_token", None)
            flash("安全驗證已更新，請重新送出表單。", "error")
            return redirect(request.path, code=303)
        if not request.path.startswith("/api/"):
            return exc
        description = str(exc.description)
        first, separator, remainder = description.partition(" ")
        error_code = first if "-" in first else "HTTP-{:03d}".format(exc.code or 500)
        message = remainder if separator else description
        response = jsonify({"error_code": error_code, "message": message})
        response.status_code = exc.code or 500
        retry_after = exc.get_response().headers.get("Retry-After")
        if retry_after is not None:
            response.headers["Retry-After"] = retry_after
        return response

    @app.errorhandler(ApplicationError)
    def application_error(exc: ApplicationError):
        if request.path.startswith("/api/"):
            return exc.response_body(), exc.http_status
        flash(exc.public_message, "error")
        target = safe_local_redirect_target(
            request.referrer,
            allowed_host=request.host,
        )
        return redirect(target or url_for("dashboard.dashboard"), code=303)

    log_event(
        LOGGER,
        logging.INFO,
        "InkTime 平台已完成初始化",
        event="platform_ready",
        details={
            "version": __version__,
            "role": container.role,
            "runtime": runtime_config.diagnostic_summary(),
        },
    )
    return app


def initialize_platform(
    app: Flask,
    *,
    database_path: Path,
    data_dir: Path,
    release_dir: Path,
    photo_dir: Path | None = None,
    testing: bool = False,
) -> Flask:
    """Backward-compatible test helper; production roots use ``create_app``."""

    if app.extensions.get("inktime_platform_initialized"):
        raise RuntimeError("initialize_platform() 不得對同一 App 重複執行")
    runtime_config = RuntimeConfig.from_sources(
        environ={},
        base_dir=data_dir.parent,
        environment="test" if testing else "development",
        database_path=database_path,
        data_dir=data_dir,
        release_dir=release_dir,
        backup_dir=data_dir / "backups",
        cache_dir=data_dir / "cache",
        photo_dir=photo_dir or data_dir.parent / "simulation_photos",
        testing=testing,
        development=not testing,
        legacy_enabled=False,
        cookie_secure=False,
    )
    container = bootstrap_services(runtime_config, role="web")
    return configure_web_application(app, runtime_config, container)
