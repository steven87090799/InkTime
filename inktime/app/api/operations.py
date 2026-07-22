from __future__ import annotations

from flask import Blueprint, abort, current_app, g, render_template, request, send_file

from inktime.app.core.paths import safe_join
from inktime.app.web.access import administrator_required, login_required
from inktime.app.workers.scanner import SCAN_MODES


bp = Blueprint("operations", __name__)


@bp.get("/diagnostics")
@login_required
def diagnostics_page():
    return render_template(
        "diagnostics.html", diagnostics=current_app.extensions["inktime_diagnostics_service"].snapshot()
    )


@bp.get("/api/v1/diagnostics/bundle")
@administrator_required
def diagnostic_bundle():
    return send_file(
        current_app.extensions["inktime_diagnostics_service"].bundle(),
        mimetype="application/zip",
        as_attachment=True,
        download_name="inktime-diagnostics.zip",
    )


@bp.get("/errors")
@login_required
def errors_page():
    with current_app.extensions["inktime_database"].session() as connection:
        errors = connection.execute(
            "SELECT * FROM job_errors ORDER BY resolved_at IS NULL DESC,last_seen_at DESC LIMIT 500"
        ).fetchall()
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


@bp.get("/maintenance")
@login_required
def maintenance_page():
    return render_template(
        "maintenance.html",
        photo_dir=current_app.config["INKTIME_PHOTO_DIR"],
    )


@bp.post("/api/v1/maintenance/scan")
@administrator_required
def enqueue_scan():
    payload = request.get_json(silent=True) or {}
    root_path = str(payload.get("root_path", "")).strip()
    if not root_path:
        abort(400, description="SCAN-001 請輸入照片資料夾路徑")
    mode = str(payload.get("mode", "incremental"))
    if mode not in SCAN_MODES:
        abort(400, description="SCAN-003 不支援的掃描模式")
    repository = current_app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(
        kind="scan",
        name=str(payload.get("name", "增量照片資料庫掃描")),
        settings={
            "root_path": root_path,
            "library_name": str(payload.get("library_name", "主要照片庫")),
            "build_thumbnails": bool(payload.get("build_thumbnails", True)),
            "mode": mode,
            "trigger_source": "api",
        },
        created_by=g.user["id"],
    )
    current_app.extensions["inktime_job_service"].start(job_id)
    return {"id": job_id, "detail_url": f"/jobs/{job_id}"}, 202


@bp.get("/api/v1/scans/<scan_id>")
@login_required
def scan_detail(scan_id: str):
    repository = current_app.extensions["inktime_photo_repository"]
    scan = repository.get_scan(scan_id)
    if scan is None:
        abort(404)
    with current_app.extensions["inktime_database"].session() as connection:
        errors = connection.execute(
            """
            SELECT id,scan_id,photo_id,stage,error_code,exception_type,retryable,masked_path,created_at
            FROM scan_errors WHERE scan_id=? ORDER BY id LIMIT 500
            """,
            (scan_id,),
        ).fetchall()
    return {"scan": dict(scan), "errors": [dict(error) for error in errors]}


@bp.post("/api/v1/scans/<scan_id>/confirm-missing")
@administrator_required
def confirm_scan_missing(scan_id: str):
    try:
        marked = current_app.extensions["inktime_photo_repository"].confirm_missing(scan_id)
    except KeyError:
        abort(404)
    except ValueError as exc:
        abort(409, description=str(exc))
    return {"scan_id": scan_id, "missing_marked": marked, "status": "confirmed"}


@bp.post("/api/v1/maintenance/virtual-display")
@administrator_required
def enqueue_virtual_display():
    """掃描固定投放資料夾並發布正式裝置也能接收的 Release。"""
    settings = current_app.extensions["inktime_settings_repository"]
    repository = current_app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(
        kind="virtual_display",
        name="虛擬墨水屏：掃描並發布",
        settings={
            "root_path": str(current_app.config["INKTIME_PHOTO_DIR"]),
            "library_name": "電子紙模擬照片",
            "profile_key": str(settings.get("render.profile", "safe_4c")),
            "quantity": max(1, min(int(settings.get("render.quantity", 5)), 50)),
        },
        created_by=g.user["id"],
    )
    current_app.extensions["inktime_job_service"].start(job_id)
    return {
        "id": job_id,
        "detail_url": f"/jobs/{job_id}",
        "receiver_url": "/virtual-display",
    }, 202
