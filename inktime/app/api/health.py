from __future__ import annotations

import os
import platform
import sys
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app

from inktime.app.web.access import administrator_required
from inktime.app.domain.photos.formats import ensure_image_codecs_registered, image_capabilities


bp = Blueprint("health", __name__)


@bp.get("/health/live")
def live():
    return {"status": "ok"}


@bp.get("/health/ready")
def ready():
    database = current_app.extensions["inktime_database"]
    with database.session() as connection:
        migrations = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        stalled = connection.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE status IN ('running','retrying','pausing')
              AND (heartbeat_at IS NULL OR heartbeat_at<?)
            """,
            ((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),),
        ).fetchone()[0]
    checks = {
        "heif_decoder": ensure_image_codecs_registered(),
        "database": database.integrity_check() == "ok",
        "release_directory": os.access(current_app.config["INKTIME_RELEASE_DIR"], os.R_OK | os.W_OK),
        "migrations": int(migrations or 0) >= 6,
        "worker": int(stalled) == 0,
        "settings": current_app.extensions["inktime_settings_repository"].get("general.timezone") is not None,
    }
    preflight = current_app.extensions["inktime_production_preflight"]
    if not preflight.healthy:
        checks["production_preflight"] = preflight.summary()
    return (
        ({"status": "ready", "checks": checks}, 200)
        if all(checks.values())
        else ({"status": "not_ready", "checks": checks}, 503)
    )


@bp.get("/health/detail")
@administrator_required
def detail():
    database = current_app.extensions["inktime_database"]
    runtime_config = current_app.extensions["inktime_runtime_config"]
    with database.session() as connection:
        heartbeats = {
            str(row["key"]).split(":", 1)[1]: str(row["updated_at"])
            for row in connection.execute(
                "SELECT key,updated_at FROM observability_state WHERE key LIKE 'heartbeat:%'"
            ).fetchall()
        }
    return {
        "status": "ok",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "database_integrity": database.integrity_check(),
        "database_bytes": database.path.stat().st_size if database.path.exists() else 0,
        "version": current_app.config.get("INKTIME_VERSION"),
        "runtime_config": runtime_config.diagnostic_summary(),
        "image_capabilities": image_capabilities(),
        "runtime_metrics": {
            "sqlite_writer_wait": database.observability(),
            "weather": current_app.extensions["inktime_weather_service"].observability(),
            "renderer_cache": current_app.extensions["inktime_render_cache"].observability(),
            "renderer_workloads": current_app.extensions["inktime_render_workload_service"].observability(),
            "webhook": current_app.extensions["inktime_notification_service"].observability(),
            "worker_child": current_app.extensions["inktime_process_boundary"].observability(),
        },
        "service_heartbeats": heartbeats,
        "production_preflight": current_app.extensions["inktime_production_preflight"].summary(),
    }
