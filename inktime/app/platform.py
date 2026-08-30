from __future__ import annotations

from datetime import timedelta
import logging
import os
from pathlib import Path
import re
import secrets
import time

from flask import Flask, flash, g, jsonify, redirect, request, session, url_for
from jinja2 import FileSystemLoader
from werkzeug.exceptions import HTTPException

from inktime import __version__
from inktime.app.api import (
    ai_traces,
    auth,
    batches,
    dashboard,
    device_pairing,
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
from inktime.app.core.logging import (
    bind_log_context,
    clear_log_context,
    log_event,
    should_log_rate_limited,
)
from inktime.app.core.errors import ApplicationError
from inktime.app.core.redirects import safe_local_redirect_target
from inktime.app.core.runtime_config import RuntimeConfig
from inktime.app.domain.analysis.traditional_chinese import to_taiwan_traditional
from inktime.app.repositories.auth import AuthRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.web.access import csrf_token, verify_csrf
from inktime.app.web.help_content import page_guide_for_endpoint


LOGGER = logging.getLogger("platform")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
QUIET_REQUEST_ENDPOINTS = {"health.live", "health.ready", "static"}


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
        ai_traces.bp,
        auth.bp,
        batches.bp,
        dashboard.bp,
        device_pairing.bp,
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
    app.jinja_env.filters["taiwan_traditional"] = to_taiwan_traditional

    @app.get("/")
    def modern_root():
        return redirect(url_for("dashboard.dashboard"))

    @app.context_processor
    def shared_template_context():
        database = container.extensions["inktime_database"]
        database_ready = False
        migration_version = None
        try:
            with database.session() as connection:
                rows = connection.execute(
                    "SELECT component,error_code,message,last_seen_at FROM job_errors "
                    "WHERE resolved_at IS NULL AND lower(severity)='critical' "
                    "ORDER BY last_seen_at DESC LIMIT 3"
                ).fetchall()
                migration_row = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()
                migration_version = int(migration_row[0] or 0) if migration_row else 0
                database_ready = True
        except Exception:
            rows = []
        return {
            "critical_alerts": rows,
            "csp_nonce": getattr(g, "csp_nonce", ""),
            "page_guide": (
                None if request.endpoint == "auth.login" else page_guide_for_endpoint(request.endpoint)
            ),
            "login_system_info": {
                "status": "可登入" if database_ready else "部分異常",
                "status_ok": database_ready,
                "version": str(app.config["INKTIME_VERSION"]),
                "revision": os.environ.get("INKTIME_GIT_REVISION", "開發版本")[:16],
                "runtime": "Docker 容器" if Path("/.dockerenv").exists() else "原生程序",
                "database": "已連線" if database_ready else "無法連線",
                "schema": str(migration_version) if migration_version is not None else "未知",
            },
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
        "pairing.request_pairing",
        "pairing.claim_pairing",
        "pairing.confirm_pairing",
        "pairing.repair_permission",
        "resilience.device_queue_manifest",
        "resilience.device_queue_ack",
        "resilience.device_queue_acks",
        "resilience.queue_item_file",
        "static",
    }
    device_endpoints = {
        "devices.latest_release",
        "devices.stock_data_up_payload",
        "devices.device_offline_schedule",
        "devices.release_file",
        "devices.report_status",
        "pairing.request_pairing",
        "pairing.claim_pairing",
        "pairing.confirm_pairing",
        "pairing.repair_permission",
        "resilience.device_queue_ack",
        "resilience.device_queue_acks",
    }

    @app.before_request
    def establish_request_context():
        supplied = request.headers.get("X-Request-ID", "").strip()
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else secrets.token_hex(16)
        g.inktime_request_started = time.perf_counter()
        g.inktime_request_id = request_id
        g.inktime_log_context_token = bind_log_context(
            request_id=request_id,
            trace_id=request_id,
            operation="http_request",
            http_method=request.method,
            process_role=container.role,
        )
        if request.endpoint not in QUIET_REQUEST_ENDPOINTS:
            log_event(
                LOGGER,
                logging.DEBUG,
                "HTTP request received",
                event="request_received",
                details={"endpoint": request.endpoint or "unmatched"},
            )

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

    @app.after_request
    def complete_request(response):
        request_id = getattr(g, "inktime_request_id", secrets.token_hex(16))
        response.headers["X-Request-ID"] = request_id
        endpoint = request.endpoint or "unmatched"
        if endpoint not in QUIET_REQUEST_ENDPOINTS:
            status = int(response.status_code)
            level = (
                logging.ERROR
                if status >= 500
                else logging.WARNING
                if status in {401, 403, 429}
                else logging.DEBUG
            )
            event = (
                "request_failed"
                if status >= 500
                else "request_rejected"
                if status >= 400
                else "request_completed"
            )
            if status >= 500 or LOGGER.isEnabledFor(logging.DEBUG) or (
                status in {401, 403, 429}
                and should_log_rate_limited(f"http:{endpoint}:{status}", interval_seconds=60)
            ):
                log_event(
                    LOGGER,
                    level,
                    "HTTP request failed"
                    if status >= 500
                    else "HTTP request rejected"
                    if status >= 400
                    else "HTTP request completed",
                    event=event,
                    http_status=status,
                    duration_ms=int(
                        (
                            time.perf_counter()
                            - getattr(g, "inktime_request_started", time.perf_counter())
                        )
                        * 1000
                    ),
                    details={
                        "endpoint": endpoint,
                        "actor_type": (
                            "user" if getattr(g, "user", None) is not None else "anonymous"
                        ),
                    },
                )
        return response

    @app.teardown_request
    def clear_request_context(exc):
        try:
            if exc is not None:
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "HTTP request raised an unexpected exception",
                    event="request_failed",
                    error_code="HTTP-500",
                    failure_class=type(exc).__name__,
                    retryable=False,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
        finally:
            token = getattr(g, "inktime_log_context_token", None)
            if token is not None:
                g.inktime_log_context_token = None
                clear_log_context(token)

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
            allowed_scheme=request.scheme,
        )
        return redirect(target or url_for("dashboard.dashboard"), code=303)

    log_event(
        LOGGER,
        logging.INFO,
        "InkTime 平台已完成初始化",
        event="platform_ready",
        process_role=container.role,
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
        cookie_secure=False,
    )
    container = bootstrap_services(runtime_config, role="web")
    return configure_web_application(app, runtime_config, container)
