from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, g, render_template, request

from inktime.app.core.logging import configure_logging
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.repositories.settings import SETTING_DEFINITIONS
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("settings", __name__)


@bp.get("/settings")
@login_required
def settings_page():
    rows = [
        row
        for row in current_app.extensions["inktime_settings_repository"].all()
        if not row["definition"].get("control_center")
    ]
    categories = {}
    for row in rows:
        categories.setdefault(row["category"], []).append(row)
    with current_app.extensions["inktime_database"].session() as connection:
        feature_flags = connection.execute("SELECT * FROM feature_flags ORDER BY key").fetchall()
    return render_template(
        "settings.html",
        categories=categories,
        settings_count=len(rows),
        feature_flags=feature_flags,
        history=current_app.extensions["inktime_settings_repository"].history(100),
        deployment={
            "docker": Path("/.dockerenv").exists(),
            "port": os.environ.get("INKTIME_PORT", "8765"),
            "data_dir": os.environ.get("INKTIME_DATA_DIR", "data"),
            "photo_dir": os.environ.get("INKTIME_PHOTO_DIR", "未設定"),
            "access_log": os.environ.get("INKTIME_ACCESS_LOG", "0") == "1",
            "revision": os.environ.get("INKTIME_GIT_REVISION", "unknown"),
        },
        webhook_token_configured=current_app.extensions[
            "inktime_notification_service"
        ].token_configured(),
    )


@bp.post("/api/v1/settings")
@administrator_required
def update_settings():
    payload = request.get_json(silent=True) or {}
    repository = current_app.extensions["inktime_settings_repository"]
    for key, value in payload.items():
        if SETTING_DEFINITIONS.get(str(key), {}).get("control_center"):
            abort(400, description=f"SET-001 請從評分控制中心修改：{key}")
        try:
            repository.update(
                str(key), value, changed_by=g.user["id"], source_ip=request.remote_addr or "unknown"
            )
        except KeyError:
            abort(400, description=f"SET-001 未知設定：{key}")
        except ValueError as exc:
            abort(400, description=f"SET-002 {exc}")
    configure_logging(settings_repository=repository)
    current_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=int(repository.get("security.session_minutes", 30))
    )
    return {"status": "ok", "updated": len(payload)}


@bp.get("/providers")
@login_required
def providers_page():
    return render_template(
        "providers.html", providers=current_app.extensions["inktime_provider_repository"].list()
    )


@bp.post("/api/v1/providers")
@administrator_required
def save_provider():
    payload = request.get_json(silent=True) or {}
    if not payload.get("base_url") or not payload.get("name"):
        abort(400, description="SET-003 Provider 名稱與 URL 不可空白")
    parsed = urlparse(str(payload["base_url"]))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        abort(400, description="SET-003 Provider URL 必須是完整的 http:// 或 https:// 位址")
    try:
        bounded = {
            "priority": (int(payload.get("priority", 100)), 1, 10000),
            "max_concurrency": (int(payload.get("max_concurrency", 2)), 1, 32),
            "timeout_seconds": (int(payload.get("timeout_seconds", 120)), 5, 600),
            "cooldown_seconds": (int(payload.get("cooldown_seconds", 300)), 1, 86400),
        }
        for field, (value, minimum, maximum) in bounded.items():
            if not minimum <= value <= maximum:
                abort(400, description=f"SET-003 {field} 超出 {minimum}–{maximum}")
            payload[field] = value
        for field in ("rate_limit_rpm", "token_limit_tpm"):
            value = payload.get(field)
            payload[field] = None if value in {None, ""} else max(1, int(value))
    except (TypeError, ValueError):
        abort(400, description="SET-003 Provider 數值欄位格式錯誤")
    provider_id = current_app.extensions["inktime_provider_repository"].save(payload, g.user["id"])
    return {"id": provider_id}, 201


@bp.post("/api/v1/providers/<provider_id>/test")
@administrator_required
def test_provider(provider_id: str):
    config = current_app.extensions["inktime_provider_repository"].get(provider_id, include_secret=True)
    if config is None:
        abort(404)
    provider = OpenAICompatibleProvider(
        name=config["name"],
        base_url=config["base_url"],
        api_key=config.get("api_key", ""),
        timeout=min(15, config["timeout_seconds"]),
        supports_json_schema=bool(config["supports_json_schema"]),
    )
    ok, message = provider.validate_config()
    return {"ok": ok, "message": message}, 200 if ok else 502


@bp.get("/costs")
@login_required
def costs_page():
    database = current_app.extensions["inktime_database"]
    with database.session() as connection:
        summary = connection.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN date(started_at)=date('now') THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) today,
                   COALESCE(SUM(CASE WHEN started_at>=datetime('now','-7 day') THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) week,
                   COALESCE(SUM(CASE WHEN strftime('%Y-%m',started_at)=strftime('%Y-%m','now') THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) month,
                   COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens
            FROM api_usage
            """
        ).fetchone()
        by_model = connection.execute(
            "SELECT provider,model,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,SUM(COALESCE(actual_cost,estimated_cost)) cost,COUNT(*) requests FROM api_usage GROUP BY provider,model ORDER BY cost DESC"
        ).fetchall()
    return render_template("costs.html", summary=summary, by_model=by_model)
