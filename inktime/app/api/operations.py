from __future__ import annotations

from flask import Blueprint, abort, current_app, g, render_template, request, send_file

from inktime.app.core.paths import safe_join
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("operations", __name__)


@bp.get("/diagnostics")
@login_required
def diagnostics_page():
    return render_template("diagnostics.html", diagnostics=current_app.extensions["inktime_diagnostics_service"].snapshot())


@bp.get("/api/v1/diagnostics/bundle")
@administrator_required
def diagnostic_bundle():
    return send_file(current_app.extensions["inktime_diagnostics_service"].bundle(), mimetype="application/zip", as_attachment=True, download_name="inktime-diagnostics.zip")


@bp.get("/errors")
@login_required
def errors_page():
    with current_app.extensions["inktime_database"].session() as connection:
        errors = connection.execute("SELECT * FROM job_errors ORDER BY resolved_at IS NULL DESC,last_seen_at DESC LIMIT 500").fetchall()
    return render_template("errors.html", errors=errors)


@bp.post("/api/v1/errors/<int:error_id>/resolve")
@administrator_required
def resolve_error(error_id: int):
    from datetime import datetime, timezone
    payload = request.get_json(silent=True) or {}
    with current_app.extensions["inktime_database"].session() as connection:
        cursor = connection.execute(
            "UPDATE job_errors SET resolved_at=?,resolution_note=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), str(payload.get("note", ""))[:1000], error_id),
        )
    if cursor.rowcount != 1:
        abort(404)
    return {"status": "ok"}


@bp.get("/backups")
@login_required
def backups_page():
    return render_template("backups.html", backups=current_app.extensions["inktime_backup_service"].list())


@bp.post("/api/v1/backups")
@administrator_required
def create_backup():
    path = current_app.extensions["inktime_backup_service"].create()
    return {"name": path.name}, 201


@bp.get("/api/v1/backups/<name>")
@administrator_required
def download_backup(name: str):
    root = current_app.extensions["inktime_backup_service"].backup_dir
    path = safe_join(root, name)
    if not path.is_file() or not path.name.startswith("inktime-backup-") or path.suffix != ".zip":
        abort(404)
    return send_file(path, mimetype="application/zip", as_attachment=True, download_name=path.name)
