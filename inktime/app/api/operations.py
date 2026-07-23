from __future__ import annotations

from flask import Blueprint, abort, current_app, g, render_template, request, send_file
from inktime.app.core.security import redact

from inktime.app.core.paths import safe_join
from inktime.app.web.access import administrator_required, login_required
from inktime.app.workers.scanner import SCAN_MODES


bp = Blueprint("operations", __name__)


def _activity_filters():
    return {
        "severity": request.args.get("severity", "").upper(),
        "component": request.args.get("component", "")[:80],
        "job_id": request.args.get("job_id", "")[:64],
        "photo_id": request.args.get("photo_id", "")[:64],
        "device_id": request.args.get("device_id", "")[:64],
        "query": request.args.get("query", "").strip()[:160],
    }


def _matches_activity(row: dict, filters: dict) -> bool:
    if filters["severity"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} and row["severity"] != filters["severity"]:
        return False
    for key in ("component", "job_id", "photo_id", "device_id"):
        if filters[key] and str(row.get(key) or "") != filters[key]:
            return False
    needle = filters["query"].casefold()
    return not needle or needle in " ".join(str(row.get(key) or "") for key in ("message", "event", "error_code")).casefold()


def _timeline_rows(connection, filters: dict, after: int) -> list[dict]:
    """Read each existing source in a small window; old rows are never copied wholesale."""
    activity_after = "WHERE id>?" if after else ""
    activity_values = (after,) if after else ()
    rows: list[dict] = []
    for row in connection.execute(
        f"SELECT id,source,source_id,severity,component,event,message,job_id,photo_id,device_id,stage,progress_done,progress_total,error_code,trace_id,details_json,created_at FROM activity_events {activity_after} ORDER BY id DESC LIMIT 200", activity_values  # noqa: S608
    ).fetchall():
        item = dict(row)
        item.update(
            {
                "source": item.get("source") or "activity_events",
                "source_id": str(item.get("source_id") or item["id"]),
                "occurred_at": item["created_at"],
            }
        )
        rows.append(item)
    if after:
        return [redact(item) for item in rows if _matches_activity(item, filters)][:200]
    for row in connection.execute("SELECT id,job_id,event,message,details_json,created_at FROM job_events ORDER BY id DESC LIMIT 200").fetchall():
        rows.append({"id": f"job:{row['id']}", "source": "job_events", "source_id": str(row["id"]), "severity": "INFO", "component": "job", "event": row["event"], "message": row["message"], "job_id": row["job_id"], "photo_id": None, "device_id": None, "stage": None, "progress_done": None, "progress_total": None, "error_code": None, "trace_id": None, "details_json": row["details_json"], "created_at": row["created_at"], "occurred_at": row["created_at"]})
    for row in connection.execute("SELECT id,device_id,level,event,error_code,message,details_json,created_at FROM device_events ORDER BY id DESC LIMIT 200").fetchall():
        level = str(row["level"]).upper()
        rows.append({"id": f"device:{row['id']}", "source": "device_events", "source_id": str(row["id"]), "severity": {"INFO": "INFO", "WARNING": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL"}.get(level, "INFO"), "component": "device", "event": row["event"], "message": row["message"], "job_id": None, "photo_id": None, "device_id": row["device_id"], "stage": None, "progress_done": None, "progress_total": None, "error_code": row["error_code"], "trace_id": None, "details_json": row["details_json"], "created_at": row["created_at"], "occurred_at": row["created_at"]})
    for row in connection.execute("SELECT id,job_id,photo_id,component,error_code,severity,message,occurrences,first_seen_at,last_seen_at,resolved_at,resolution_note FROM job_errors ORDER BY last_seen_at DESC,id DESC LIMIT 200").fetchall():
        rows.append({"id": f"error:{row['id']}", "source": "job_errors", "source_id": str(row["id"]), "severity": str(row["severity"]).upper(), "component": row["component"], "event": "error_resolved" if row["resolved_at"] else "error", "message": row["message"], "job_id": row["job_id"], "photo_id": row["photo_id"], "device_id": None, "stage": None, "progress_done": None, "progress_total": None, "error_code": row["error_code"], "trace_id": None, "details_json": {"occurrences": row["occurrences"], "first_seen_at": row["first_seen_at"], "resolved_at": row["resolved_at"], "resolution_note": row["resolution_note"]}, "created_at": row["last_seen_at"], "occurred_at": row["last_seen_at"]})
    unique = {(item["source"], item["source_id"]): item for item in rows}
    ordered = sorted(unique.values(), key=lambda item: (str(item["occurred_at"]), str(item["source"]), str(item["source_id"])), reverse=True)
    return [redact(item) for item in ordered if _matches_activity(item, filters)][:200]


@bp.get("/activity")
@login_required
def activity_page():
    return render_template("activity.html", settings=current_app.extensions["inktime_settings_repository"])


@bp.get("/api/v1/activity")
@login_required
def activity_feed():
    try:
        after = max(0, int(request.args.get("after", 0) or 0))
    except ValueError:
        abort(400, description="ACTIVITY-001 cursor 格式錯誤")
    filters = _activity_filters()
    with current_app.extensions["inktime_database"].session() as connection:
        events = _timeline_rows(connection, filters, after)
        summary = connection.execute("SELECT COUNT(*) running_jobs FROM jobs WHERE status IN ('running','retrying','pausing')").fetchone()
        queue = connection.execute("SELECT COUNT(*) FROM job_items WHERE status IN ('pending','running')").fetchone()[0]
        issues = connection.execute("SELECT upper(severity) severity,COUNT(*) count FROM job_errors WHERE resolved_at IS NULL GROUP BY upper(severity)").fetchall()
    levels = {row["severity"]: int(row["count"]) for row in issues}
    status = "嚴重故障" if levels.get("CRITICAL") else "部分失敗" if levels.get("ERROR") else "有警告" if levels.get("WARNING") else "正常"
    return {"events": events, "summary": {"status": status, "running_jobs": int(summary["running_jobs"]), "queue": int(queue), "issues": levels}}


@bp.get("/api/v1/activity/download")
@administrator_required
def activity_download():
    import json

    with current_app.extensions["inktime_database"].session() as connection:
        rows = _timeline_rows(connection, _activity_filters(), 0)
    return current_app.response_class(
        json.dumps(rows, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=inktime-activity.json"},
    )


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


@bp.get("/schedules")
@login_required
def schedules_page():
    tasks = current_app.extensions["inktime_schedule_repository"].list()
    labels = {
        "incremental_scan": "沿用既有 Scanner 的增量入口；不重新走訪已完成的分析。",
        "full_reconcile": "完整掃描仍受 Missing 安全比例與人工確認機制保護。",
        "display_prepare": "僅建立背景換圖／渲染工作；ESP32 下載不會等待掃描、AI 或渲染。",
        "ai_schedule": "只安排既有 AI 工作入口，不改變分析內容與 Schema。",
        "cache_cleanup": "只處理縮圖快取，不會刪除原始照片、正式 Release 或目前使用的 Release。",
    }
    return render_template("schedules.html", tasks=tasks, labels=labels)


@bp.get("/api/v1/schedules")
@administrator_required
def list_schedules():
    return {"tasks": current_app.extensions["inktime_schedule_repository"].list()}


@bp.patch("/api/v1/schedules/<key>")
@administrator_required
def update_schedule(key: str):
    payload = request.get_json(silent=True) or {}
    try:
        task = current_app.extensions["inktime_schedule_repository"].update(
            key, payload, str(current_app.extensions["inktime_settings_repository"].get("general.timezone"))
        )
    except KeyError:
        abort(404)
    except ValueError as exc:
        abort(400, description=f"SCHEDULE-002 {exc}")
    return {"task": task}


@bp.post("/api/v1/schedules/<key>/run")
@administrator_required
def run_schedule_now(key: str):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    task = current_app.extensions["inktime_schedule_repository"].get(key)
    if task is None:
        abort(404)
    from inktime.app.workers.scheduler import SchedulerRunner

    now = datetime.now(
        ZoneInfo(str(current_app.extensions["inktime_settings_repository"].get("general.timezone")))
    )
    try:
        SchedulerRunner(current_app)._enqueue_task(task, now, force=True)
    except Exception as exc:
        current_app.extensions["inktime_schedule_repository"].record_failure(task, str(exc), now)
        abort(409, description=f"SCHEDULE-003 {exc}")
    return {"status": "enqueued"}, 202


def _active_thumbnail_hashes() -> set[str]:
    with current_app.extensions["inktime_database"].session() as connection:
        return {
            str(row[0]).casefold()
            for row in connection.execute(
                "SELECT DISTINCT sha256 FROM photos WHERE lifecycle_status='active' AND sha256 IS NOT NULL"
            )
        }


@bp.post("/api/v1/maintenance/cache/estimate")
@administrator_required
def estimate_cache_cleanup():
    payload = request.get_json(silent=True) or {}
    cache = current_app.extensions["inktime_thumbnail_cache"]
    return cache.estimate_cleanup(
        max_bytes=int(payload.get("max_bytes", 5 * 1024 * 1024 * 1024)),
        retention_days=int(payload.get("retention_days", 30)),
        active_hashes=_active_thumbnail_hashes(),
    )


@bp.post("/api/v1/maintenance/cache/cleanup")
@administrator_required
def cleanup_cache():
    payload = request.get_json(silent=True) or {}
    cache = current_app.extensions["inktime_thumbnail_cache"]
    return cache.cleanup(
        max_bytes=int(payload.get("max_bytes", 5 * 1024 * 1024 * 1024)),
        retention_days=int(payload.get("retention_days", 30)),
        active_hashes=_active_thumbnail_hashes(),
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
