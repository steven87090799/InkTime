from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import secrets
from typing import Literal

from inktime.app.core.locks import FcntlLockProvider, LockProvider
from inktime.app.core.logging import configure_logging
from inktime.app.core.runtime_config import RuntimeConfig, resolve_runtime_config
from inktime.app.db import Database, backfill_photo_capture_dates, migrate
from inktime.app.domain.photos import LocationResolver, ThumbnailCache
from inktime.app.domain.rendering import AtomicReleasePublisher, FontManager
from inktime.app.repositories.auth import AuthRepository
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.repositories.jobs import JobRepository
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.providers import ProviderRepository
from inktime.app.repositories.render_candidates import RenderCandidateRepository
from inktime.app.repositories.schedules import ScheduledTaskRepository
from inktime.app.repositories.scoring import ScoringProfileRepository
from inktime.app.repositories.settings import SecretStore, SettingsRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.analysis import PhotoAnalysisService
from inktime.app.services.backups import BackupService
from inktime.app.services.budgets import BudgetService
from inktime.app.services.device_energy import DeviceEnergyService
from inktime.app.services.diagnostics import DiagnosticsService
from inktime.app.services.display_prepare import DisplayPreparationService
from inktime.app.services.jobs import JobService
from inktime.app.services.notifications import DeviceNotificationService
from inktime.app.services.observability import ObservabilityService
from inktime.app.services.providers import ProviderService
from inktime.app.services.release_coordinator import ReleaseCoordinator
from inktime.app.services.render_cache import BoundedRenderCache
from inktime.app.services.render_workloads import RenderWorkloadService
from inktime.app.services.rendering import RenderService
from inktime.app.services.scoring_lab import ScoringLabService
from inktime.app.services.weather import WeatherService
from inktime.app.workers.process_boundary import KillableProcessBoundary


RuntimeRole = Literal["web", "worker", "scheduler"]


@dataclass
class ServiceContainer:
    """Process-local services. It deliberately has no Flask dependency."""

    runtime_config: RuntimeConfig
    role: RuntimeRole
    extensions: dict[str, object]
    session_secret: str = field(repr=False)

    def close(self) -> None:
        boundary = self.extensions.get("inktime_process_boundary")
        if boundary is not None:
            boundary.shutdown()  # type: ignore[attr-defined]
        runtime_lock = self.extensions.get("inktime_runtime_lock")
        if runtime_lock is not None:
            runtime_lock.close()  # type: ignore[attr-defined]


def _persistent_secret(
    runtime_config: RuntimeConfig, lock_provider: LockProvider
) -> str:
    if runtime_config.testing:
        return "test-secret-not-for-production"
    import os

    configured = os.environ.get("INKTIME_SECRET_KEY", "").strip()
    if configured:
        return configured
    path = runtime_config.data_dir / "session.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_provider.exclusive(path.with_suffix(".key.lock")):
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(64)
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)
        return value


def bootstrap_services(
    runtime_config: RuntimeConfig | None = None,
    *,
    role: RuntimeRole,
    lock_provider: LockProvider | None = None,
) -> ServiceContainer:
    """Build one process role from the same resolved deployment configuration."""

    config = resolve_runtime_config(runtime_config)
    locks = lock_provider or FcntlLockProvider()
    configure_logging()

    # Filesystem creation is an explicit bootstrap step, never an import side effect.
    for path in (config.data_dir, config.cache_dir, config.backup_dir, config.release_dir):
        path.mkdir(parents=True, exist_ok=True)

    database = Database(config.database_path)
    migrate(database, None if config.testing else config.backup_dir)
    backfill_photo_capture_dates(database)
    extensions: dict[str, object] = {
        "inktime_runtime_config": config,
        "inktime_database": database,
    }
    if not config.testing:
        extensions["inktime_runtime_lock"] = database.acquire_runtime_lock(exclusive=False)

    secret = _persistent_secret(config, locks)
    settings_repository = SettingsRepository(database)
    settings_repository.ensure_defaults()
    schedule_repository = ScheduledTaskRepository(database)
    schedule_repository.ensure_defaults(config.timezone)
    configure_logging(settings_repository=settings_repository)

    job_repository = JobRepository(database)
    job_service = JobService(job_repository)
    secret_store = SecretStore(database, secret)
    diagnostics_service = DiagnosticsService(
        database,
        config.data_dir,
        config.cache_dir / "thumbnails",
        settings_repository=settings_repository,
    )
    notification_service = DeviceNotificationService(
        database, settings_repository, secret_store
    )
    observability_service = ObservabilityService(
        database, settings_repository, diagnostics_service
    )
    extensions.update(
        {
            "inktime_settings_repository": settings_repository,
            "inktime_schedule_repository": schedule_repository,
            "inktime_job_repository": job_repository,
            "inktime_job_service": job_service,
            "inktime_secret_store": secret_store,
            "inktime_notification_service": notification_service,
            "inktime_diagnostics_service": diagnostics_service,
            "inktime_observability_service": observability_service,
            "inktime_backup_service": BackupService(database, config.backup_dir),
        }
    )

    if role == "scheduler":
        return ServiceContainer(config, role, extensions, secret)

    device_repository = DeviceRepository(database, secret)
    photo_repository = PhotoRepository(database)
    provider_repository = ProviderRepository(database, secret_store)
    scoring_repository = ScoringProfileRepository(database, settings_repository)
    scoring_repository.ensure_initial()
    usage_repository = UsageRepository(database)
    thumbnail_cache = ThumbnailCache(config.cache_dir / "thumbnails")
    budget_service = BudgetService(database, settings_repository)
    provider_service = ProviderService(provider_repository, settings_repository)
    process_boundary = KillableProcessBoundary(max_processes=config.worker_concurrency)
    analysis_service = PhotoAnalysisService(
        photo_repository,
        usage_repository,
        thumbnail_cache,
        budget_service,
        settings_repository,
        observability_service,
        process_boundary,
    )
    font_manager = FontManager(config.data_dir / "fonts")
    location_resolver = LocationResolver(
        Path(__file__).resolve().parents[2] / "data" / "world_cities_zh.csv"
    )
    release_publisher = AtomicReleasePublisher(config.release_dir)
    observability_service.publisher = release_publisher
    render_candidate_repository = RenderCandidateRepository(database)
    render_cache = BoundedRenderCache(config.cache_dir / "renderer")
    render_workload_service = RenderWorkloadService(
        config.cache_dir / "render-workloads",
        release_publisher,
        device_repository,
        config.release_dir,
        settings_repository,
        process_boundary,
        job_repository,
    )
    release_coordinator = ReleaseCoordinator(database, release_publisher)
    weather_service = WeatherService(settings_repository)
    render_service = RenderService(
        database,
        photo_repository,
        settings_repository,
        font_manager,
        release_publisher,
        render_candidate_repository,
        release_coordinator,
        location_resolver,
        weather_service,
        observability_service,
    )
    extensions.update(
        {
            "inktime_device_repository": device_repository,
            "inktime_photo_repository": photo_repository,
            "inktime_provider_repository": provider_repository,
            "inktime_scoring_repository": scoring_repository,
            "inktime_usage_repository": usage_repository,
            "inktime_thumbnail_cache": thumbnail_cache,
            "inktime_budget_service": budget_service,
            "inktime_provider_service": provider_service,
            "inktime_process_boundary": process_boundary,
            "inktime_analysis_service": analysis_service,
            "inktime_scoring_lab_service": ScoringLabService(
                provider_service,
                scoring_repository,
                settings_repository,
                usage_repository,
                budget_service,
            ),
            "inktime_font_manager": font_manager,
            "inktime_location_resolver": location_resolver,
            "inktime_release_publisher": release_publisher,
            "inktime_render_candidate_repository": render_candidate_repository,
            "inktime_render_cache": render_cache,
            "inktime_render_workload_service": render_workload_service,
            "inktime_release_coordinator": release_coordinator,
            "inktime_weather_service": weather_service,
            "inktime_render_service": render_service,
            "inktime_display_preparation_service": DisplayPreparationService(
                database, render_service
            ),
        }
    )
    if role == "web":
        extensions.update(
            {
                "inktime_auth_repository": AuthRepository(database),
                "inktime_device_energy_service": DeviceEnergyService(device_repository),
                "inktime_release_reconciliation": release_coordinator.reconcile(),
            }
        )
    return ServiceContainer(config, role, extensions, secret)
