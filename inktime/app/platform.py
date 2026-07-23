from __future__ import annotations

from datetime import timedelta
import fcntl
import logging
from pathlib import Path
import os
import secrets

from flask import Flask, flash, g, redirect, request, session, url_for
from jinja2 import BaseLoader, ChoiceLoader, FileSystemLoader
from werkzeug.exceptions import HTTPException

from inktime import __version__
from inktime.app.api import (
    auth,
    dashboard,
    devices,
    health,
    jobs,
    notifications,
    operations,
    photos,
    rendering,
    scoring,
    settings,
)
from inktime.app.db import Database, migrate
from inktime.app.repositories.auth import AuthRepository
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.repositories.jobs import JobRepository
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.render_candidates import RenderCandidateRepository
from inktime.app.repositories.providers import ProviderRepository
from inktime.app.repositories.scoring import ScoringProfileRepository
from inktime.app.repositories.schedules import ScheduledTaskRepository
from inktime.app.repositories.settings import SecretStore, SettingsRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.jobs import JobService
from inktime.app.services.backups import BackupService
from inktime.app.services.diagnostics import DiagnosticsService
from inktime.app.domain.photos import LocationResolver, ThumbnailCache
from inktime.app.domain.rendering import AtomicReleasePublisher, FontManager
from inktime.app.services.rendering import RenderService
from inktime.app.services.release_coordinator import ReleaseCoordinator
from inktime.app.services.display_prepare import DisplayPreparationService
from inktime.app.services.analysis import PhotoAnalysisService
from inktime.app.services.budgets import BudgetService
from inktime.app.services.providers import ProviderService
from inktime.app.services.scoring_lab import ScoringLabService
from inktime.app.services.notifications import DeviceNotificationService
from inktime.app.services.device_energy import DeviceEnergyService
from inktime.app.services.weather import WeatherService
from inktime.app.services.observability import ObservabilityService
from inktime.app.core.logging import configure_logging, log_event
from inktime.app.web.access import csrf_token, verify_csrf


LOGGER = logging.getLogger("platform")


def _persistent_secret(path: Path) -> str:
    configured = os.environ.get("INKTIME_SECRET_KEY", "").strip()
    if configured:
        return configured
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
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
    photo_dir: Path | None = None,
    testing: bool = False,
) -> Flask:
    configure_logging()
    data_dir.mkdir(parents=True, exist_ok=True)
    release_dir.mkdir(parents=True, exist_ok=True)
    database = Database(database_path)
    migrate(database, None if testing else data_dir / "backups")
    if not testing:
        # 每個正式程序持有 shared runtime lock；離線還原必須等所有程序停止。
        app.extensions["inktime_runtime_lock"] = database.acquire_runtime_lock(exclusive=False)
    secret = "test-secret-not-for-production" if testing else _persistent_secret(data_dir / "session.key")

    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.environ.get("INKTIME_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        INKTIME_RELEASE_DIR=release_dir.resolve(),
        INKTIME_PHOTO_DIR=(photo_dir or data_dir.parent / "simulation_photos").resolve(),
        INKTIME_VERSION=__version__,
        TESTING=testing,
    )
    app.extensions["inktime_database"] = database
    app.extensions["inktime_auth_repository"] = AuthRepository(database)
    app.extensions["inktime_device_repository"] = DeviceRepository(database, secret)
    app.extensions["inktime_device_energy_service"] = DeviceEnergyService(
        app.extensions["inktime_device_repository"]
    )
    app.extensions["inktime_job_repository"] = JobRepository(database)
    app.extensions["inktime_job_service"] = JobService(app.extensions["inktime_job_repository"])
    settings_repository = SettingsRepository(database)
    settings_repository.ensure_defaults()
    schedule_repository = ScheduledTaskRepository(database)
    schedule_repository.ensure_defaults(str(settings_repository.get("general.timezone", "Asia/Taipei")))
    configure_logging(settings_repository=settings_repository)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=int(settings_repository.get("security.session_minutes", 30))
    )
    secret_store = SecretStore(database, secret)
    app.extensions["inktime_settings_repository"] = settings_repository
    app.extensions["inktime_schedule_repository"] = schedule_repository
    scoring_repository = ScoringProfileRepository(database, settings_repository)
    scoring_repository.ensure_initial()
    app.extensions["inktime_scoring_repository"] = scoring_repository
    app.extensions["inktime_secret_store"] = secret_store
    app.extensions["inktime_notification_service"] = DeviceNotificationService(
        database, settings_repository, secret_store
    )
    app.extensions["inktime_provider_repository"] = ProviderRepository(database, secret_store)
    app.extensions["inktime_photo_repository"] = PhotoRepository(database)
    app.extensions["inktime_render_candidate_repository"] = RenderCandidateRepository(database)
    app.extensions["inktime_usage_repository"] = UsageRepository(database)
    app.extensions["inktime_thumbnail_cache"] = ThumbnailCache(data_dir / "cache" / "thumbnails")
    budget_service = BudgetService(database, settings_repository)
    app.extensions["inktime_budget_service"] = budget_service
    app.extensions["inktime_provider_service"] = ProviderService(
        app.extensions["inktime_provider_repository"], settings_repository
    )
    app.extensions["inktime_analysis_service"] = PhotoAnalysisService(
        app.extensions["inktime_photo_repository"],
        app.extensions["inktime_usage_repository"],
        app.extensions["inktime_thumbnail_cache"],
        budget_service,
        settings_repository,
    )
    app.extensions["inktime_scoring_lab_service"] = ScoringLabService(
        app.extensions["inktime_provider_service"],
        scoring_repository,
        settings_repository,
        app.extensions["inktime_usage_repository"],
        budget_service,
    )
    app.extensions["inktime_backup_service"] = BackupService(database, data_dir / "backups")
    app.extensions["inktime_diagnostics_service"] = DiagnosticsService(
        database,
        data_dir,
        data_dir / "cache" / "thumbnails",
        settings_repository=settings_repository,
    )
    app.extensions["inktime_observability_service"] = ObservabilityService(database, settings_repository, app.extensions["inktime_diagnostics_service"])
    font_manager = FontManager(data_dir / "fonts")
    location_resolver = LocationResolver(Path(__file__).resolve().parents[2] / "data" / "world_cities_zh.csv")
    release_publisher = AtomicReleasePublisher(release_dir)
    app.extensions["inktime_observability_service"].publisher = release_publisher
    app.extensions["inktime_font_manager"] = font_manager
    app.extensions["inktime_location_resolver"] = location_resolver
    app.extensions["inktime_release_publisher"] = release_publisher
    release_coordinator = ReleaseCoordinator(database, release_publisher)
    app.extensions["inktime_release_coordinator"] = release_coordinator
    weather_service = WeatherService(settings_repository)
    app.extensions["inktime_weather_service"] = weather_service
    app.extensions["inktime_render_service"] = RenderService(
        database,
        app.extensions["inktime_photo_repository"],
        settings_repository,
        font_manager,
        release_publisher,
        app.extensions["inktime_render_candidate_repository"],
        release_coordinator,
        location_resolver,
        weather_service,
    )
    app.extensions["inktime_display_preparation_service"] = DisplayPreparationService(
        database, app.extensions["inktime_render_service"]
    )
    app.extensions["inktime_release_reconciliation"] = release_coordinator.reconcile()

    web_root = Path(__file__).resolve().parent / "web"
    loaders: list[BaseLoader] = [FileSystemLoader(str(web_root / "templates"))]
    if app.jinja_loader is not None:
        loaders.insert(0, app.jinja_loader)
    app.jinja_loader = ChoiceLoader(loaders)
    app.static_folder = str(web_root / "static")

    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(devices.bp)
    app.register_blueprint(health.bp)
    app.register_blueprint(jobs.bp)
    app.register_blueprint(notifications.bp)
    app.register_blueprint(photos.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(scoring.bp)
    app.register_blueprint(operations.bp)
    app.register_blueprint(rendering.bp)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.context_processor
    def critical_alerts():
        try:
            with database.session() as connection:
                rows = connection.execute("SELECT component,error_code,message,last_seen_at FROM job_errors WHERE resolved_at IS NULL AND lower(severity)='critical' ORDER BY last_seen_at DESC LIMIT 3").fetchall()
        except Exception:
            rows = []
        return {"critical_alerts": rows}

    public_endpoints = {
        "auth.setup",
        "auth.login",
        "health.live",
        "health.ready",
        "devices.latest_release",
        "devices.release_file",
        "devices.report_status",
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
            "devices.report_status",
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
        return {"error_code": error_code, "message": message}, exc.code or 500

    log_event(
        LOGGER,
        logging.INFO,
        "InkTime 平台已完成初始化",
        event="platform_ready",
        details={"version": __version__, "testing": testing},
    )
    return app
