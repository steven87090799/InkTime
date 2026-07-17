from __future__ import annotations

import os
import platform
import sys

from flask import Blueprint, current_app

from inktime.app.web.access import administrator_required


bp = Blueprint("health", __name__)


@bp.get("/health/live")
def live():
    return {"status": "ok"}


@bp.get("/health/ready")
def ready():
    database = current_app.extensions["inktime_database"]
    checks = {
        "database": database.integrity_check() == "ok",
        "release_directory": os.access(current_app.config["INKTIME_RELEASE_DIR"], os.R_OK | os.W_OK),
        "migrations": True,
        "settings": True,
    }
    return ({"status": "ready", "checks": checks}, 200) if all(checks.values()) else ({"status": "not_ready", "checks": checks}, 503)


@bp.get("/health/detail")
@administrator_required
def detail():
    database = current_app.extensions["inktime_database"]
    return {
        "status": "ok",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "database_integrity": database.integrity_check(),
        "database_bytes": database.path.stat().st_size if database.path.exists() else 0,
        "version": current_app.config.get("INKTIME_VERSION"),
    }
