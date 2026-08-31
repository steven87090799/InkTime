from __future__ import annotations

from flask import Blueprint, current_app, render_template

from inktime.app.web.ai_readiness import MODE_LABELS, ai_readiness_snapshot
from inktime.app.web.access import login_required


bp = Blueprint("dashboard", __name__)


def _configured_step(
    number: int,
    title: str,
    *,
    complete: bool,
    current: str,
    detail: str,
    action_label: str,
    action_url: str,
    attention: bool = False,
    required: bool = True,
) -> dict:
    return {
        "number": number,
        "title": title,
        "complete": complete,
        "attention": bool(complete and attention),
        "current": current,
        "detail": detail,
        "action_label": action_label,
        "action_url": action_url,
        "required": required,
    }


def _setup_overview(connection) -> dict:
    settings = current_app.extensions["inktime_settings_repository"]
    providers = current_app.extensions["inktime_provider_repository"]
    provider_service = current_app.extensions["inktime_provider_service"]
    readiness = ai_readiness_snapshot(settings, providers, provider_service)
    routes = provider_service.usable_route_snapshot()

    library_count = int(
        connection.execute("SELECT COUNT(*) FROM libraries WHERE enabled=1").fetchone()[0]
    )
    photo_count = int(
        connection.execute("SELECT COUNT(*) FROM photos WHERE lifecycle_status='active'").fetchone()[0]
    )
    last_scan = connection.execute(
        "SELECT status,completed_at,failed_count FROM scan_runs ORDER BY started_at DESC,id DESC LIMIT 1"
    ).fetchone()
    scan_ready = bool(last_scan and str(last_scan["status"]) in {"completed", "completed_with_warnings"})

    mode = str(readiness["execution_mode"])
    ai_mode = str(settings.get("analysis.ai_mode", "off"))
    strategy = str(settings.get("analysis.strategy", "single"))
    ai_mode_label = {
        "off": "關閉",
        "top_candidates": "只分析最佳候選",
        "full_library": "完整照片庫",
    }.get(ai_mode, ai_mode)
    strategy_label = {
        "local": "僅本機",
        "single": "單次完整分析",
        "smart_two_stage": "智慧兩階段",
    }.get(strategy, strategy)

    route_labels = [
        f"{str(route.get('display_name') or route['provider_id'])} / {str(route.get('model') or readiness['model'])}"
        for route in routes
    ]
    daily_limit = int(settings.get("analysis.ai_daily_photo_limit", 0) or 0)
    monthly_limit = int(settings.get("analysis.ai_monthly_photo_limit", 0) or 0)
    daily_stop = float(settings.get("budget.daily_stop", 0) or 0)
    monthly_stop = float(settings.get("budget.monthly_stop", 0) or 0)

    devices = [dict(row) for row in current_app.extensions["inktime_device_repository"].list()]
    paired_devices = [
        row for row in devices if bool(row.get("enabled")) and str(row.get("pairing_state")) == "paired"
    ]
    pending_devices = [row for row in devices if str(row.get("pairing_state")) == "pairing_pending"]
    seen_devices = [row for row in paired_devices if row.get("last_seen_at")]
    device_profiles = sorted({str(row.get("panel_profile") or "未指定") for row in paired_devices})

    schedules = current_app.extensions["inktime_schedule_repository"].list()
    enabled_schedules = [task for task in schedules if bool(task.get("enabled"))]
    schedule_warnings = [
        task
        for task in enabled_schedules
        if task.get("error_status") or (task.get("last_failure") and not task.get("last_success"))
    ]

    backup_enabled = bool(settings.get("backup.schedule_enabled", True))
    backup_retention = int(settings.get("backup.retention", 14) or 14)
    backups = list(current_app.extensions["inktime_backup_service"].list())
    latest_backup = backups[0].name if backups else "尚未建立"

    steps = (
        _configured_step(
            1,
            "照片來源與掃描",
            complete=library_count > 0 and photo_count > 0 and scan_ready,
            attention=bool(last_scan and int(last_scan["failed_count"] or 0) > 0),
            current=f"{library_count} 個照片庫、{photo_count} 張有效照片",
            detail=(
                f"最近掃描：{str(last_scan['status'])} · {str(last_scan['completed_at'] or '尚未完成')}"
                if last_scan
                else "尚未執行照片掃描。"
            ),
            action_label="前往照片庫",
            action_url="/photos",
        ),
        _configured_step(
            2,
            "分析方式",
            complete=mode != "disabled" and bool(strategy),
            attention=mode != "automatic_ai",
            current=f"{MODE_LABELS.get(mode, mode)} · {ai_mode_label} · {strategy_label}",
            detail="決定是否自動送入 AI、分析哪些照片，以及每張照片採用哪種分析流程。",
            action_label="調整分析設定",
            action_url="/settings?search=分析執行模式",
        ),
        _configured_step(
            3,
            "Vision Provider 與模型",
            complete=bool(routes),
            current="、".join(route_labels) if route_labels else "尚無可用 Provider",
            detail=(
                f"目前有 {len(routes)} 條可用路由；新建立的 AI 工作會使用這裡顯示的 Provider／模型。"
                if routes
                else "必須啟用支援 Vision 的 Provider，填妥 Base URL、API Key 與模型。"
            ),
            action_label="設定模型服務",
            action_url="/providers",
        ),
        _configured_step(
            4,
            "用量限制與預算保護",
            complete=all(value > 0 for value in (daily_limit, monthly_limit, daily_stop, monthly_stop)),
            current=(
                f"每日 {daily_limit} 張／每月 {monthly_limit} 張 · "
                f"每日停用 US$ {daily_stop:g}／每月停用 US$ {monthly_stop:g}"
            ),
            detail="達到照片數或成本上限時會停止新增模型請求，避免失控使用。",
            action_label="調整限制與預算",
            action_url="/settings?search=預算",
        ),
        _configured_step(
            5,
            "電子紙裝置",
            complete=bool(paired_devices),
            attention=bool(pending_devices) or (bool(paired_devices) and not seen_devices),
            current=(
                f"已配對 {len(paired_devices)} 台、待配對 {len(pending_devices)} 台"
                + (f" · 面板：{'、'.join(device_profiles)}" if device_profiles else "")
            ),
            detail=(
                f"已有 {len(seen_devices)} 台回報上線。"
                if seen_devices
                else "裝置設定已存在，但目前沒有已配對裝置回報上線。"
            ),
            action_label="查看裝置與配對",
            action_url="/devices",
        ),
        _configured_step(
            6,
            "自動排程",
            complete=bool(enabled_schedules),
            attention=bool(schedule_warnings),
            current=f"{len(enabled_schedules)} 個排程已啟用",
            detail=(
                f"其中 {len(schedule_warnings)} 個排程需要檢查最近失敗狀態。"
                if schedule_warnings
                else "掃描、AI、渲染與維護會依排程自動執行。"
            ),
            action_label="查看排程",
            action_url="/schedules",
            required=False,
        ),
        _configured_step(
            7,
            "備份與保留",
            complete=backup_enabled and bool(backups),
            attention=backup_enabled and not backups,
            current=f"自動備份{'已啟用' if backup_enabled else '未啟用'} · 保留 {backup_retention} 份",
            detail=f"最近備份：{latest_backup}",
            action_label="查看與建立備份",
            action_url="/backups",
            required=False,
        ),
    )
    completed = sum(1 for step in steps if step["complete"])
    missing = sum(1 for step in steps if not step["complete"])
    attention = sum(1 for step in steps if step["attention"])
    runtime = current_app.extensions["inktime_runtime_config"]
    return {
        "steps": steps,
        "completed": completed,
        "total": len(steps),
        "missing": missing,
        "attention": attention,
        "percent": round(completed / len(steps) * 100),
        "environment": str(runtime.environment),
        "photo_dir": str(runtime.photo_dir),
        "data_dir": str(runtime.data_dir),
        "timezone": str(runtime.timezone),
    }


@bp.get("/dashboard")
@login_required
def dashboard():
    database = current_app.extensions["inktime_database"]
    with database.session() as connection:
        counts = {
            "photos": connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
            "analyzed": connection.execute("SELECT COUNT(*) FROM photos WHERE status='analyzed'").fetchone()[
                0
            ],
            "failed": connection.execute("SELECT COUNT(*) FROM photos WHERE status='failed'").fetchone()[0],
            "duplicates": connection.execute(
                "SELECT COUNT(*) FROM photos WHERE duplicate_group_id IS NOT NULL"
            ).fetchone()[0],
            "running_jobs": connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('preparing','running','pausing','retrying')"
            ).fetchone()[0],
            "today_tokens": connection.execute(
                "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM api_usage WHERE date(started_at)=date('now')"
            ).fetchone()[0],
            "month_cost": connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN cost_source<>'unknown' THEN COALESCE(actual_cost, estimated_cost) ELSE 0 END),0) FROM api_usage WHERE strftime('%Y-%m',started_at)=strftime('%Y-%m','now')"
            ).fetchone()[0],
            "month_unknown_count": connection.execute(
                """
                SELECT COUNT(*) FROM api_usage
                WHERE cost_source='unknown'
                  AND strftime('%Y-%m',started_at)=strftime('%Y-%m','now')
                  AND (
                      COALESCE(input_tokens,0)>0 OR COALESCE(output_tokens,0)>0
                      OR COALESCE(cached_tokens,0)>0 OR COALESCE(reasoning_tokens,0)>0
                      OR COALESCE(cache_write_tokens,0)>0 OR COALESCE(request_body_bytes,0)>0
                      OR COALESCE(image_bytes,0)>0 OR COALESCE(actual_cost,0)>0
                      OR COALESCE(estimated_cost,0)>0
                  )
                """
            ).fetchone()[0],
        }
        recent_errors = connection.execute(
            "SELECT error_code, message, last_seen_at, occurrences FROM job_errors WHERE resolved_at IS NULL ORDER BY last_seen_at DESC LIMIT 5"
        ).fetchall()
        severities = connection.execute(
            "SELECT lower(severity) severity,COUNT(*) count FROM job_errors WHERE resolved_at IS NULL GROUP BY lower(severity)"
        ).fetchall()
        setup = _setup_overview(connection)
    reserve = float(
        current_app.extensions["inktime_settings_repository"].get("budget.unknown_request_reserve", 0.25)
    )
    counts["month_unknown_reserve"] = int(counts["month_unknown_count"]) * reserve
    counts["cost_complete"] = int(counts["month_unknown_count"]) == 0
    issues = {str(row["severity"]): int(row["count"]) for row in severities}
    status_levels = (
        ("critical", "第 5 級", "嚴重故障", "需要立即處理，核心服務、儲存或資料安全可能受影響。"),
        ("error", "第 4 級", "部分失敗", "已有功能失敗，但其他功能可能仍可使用。"),
        ("warning", "第 3 級", "有警告", "目前仍可使用，但有狀態需要盡快檢查。"),
        ("info", "第 2 級", "有提示", "沒有失敗；有一般事件或建議可查看。"),
    )
    status = next(
        (
            {
                "level": level,
                "rank": rank,
                "label": label,
                "description": description,
                "count": int(issues.get(level, 0)),
            }
            for level, rank, label, description in status_levels
            if issues.get(level)
        ),
        {
            "level": "ok",
            "rank": "第 1 級",
            "label": "正常",
            "description": "目前沒有未解決的警告、錯誤或嚴重故障。",
            "count": 0,
        },
    )
    return render_template(
        "dashboard.html",
        counts=counts,
        recent_errors=recent_errors,
        issues=issues,
        system_status=status,
        setup=setup,
    )
