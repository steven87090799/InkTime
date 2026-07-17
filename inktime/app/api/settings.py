from __future__ import annotations

from flask import Blueprint, abort, current_app, g, render_template, request

from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("settings", __name__)


@bp.get("/settings")
@login_required
def settings_page():
    rows = current_app.extensions["inktime_settings_repository"].all()
    categories = {}
    for row in rows:
        categories.setdefault(row["category"], []).append(row)
    return render_template("settings.html", categories=categories)


@bp.post("/api/v1/settings")
@administrator_required
def update_settings():
    payload = request.get_json(silent=True) or {}
    repository = current_app.extensions["inktime_settings_repository"]
    for key, value in payload.items():
        try:
            repository.update(str(key), value, changed_by=g.user["id"], source_ip=request.remote_addr or "unknown")
        except KeyError:
            abort(400, description=f"SET-001 未知設定：{key}")
        except ValueError as exc:
            abort(400, description=f"SET-002 {exc}")
    return {"status": "ok", "updated": len(payload)}


@bp.get("/providers")
@login_required
def providers_page():
    return render_template("providers.html", providers=current_app.extensions["inktime_provider_repository"].list())


@bp.post("/api/v1/providers")
@administrator_required
def save_provider():
    payload = request.get_json(silent=True) or {}
    if not payload.get("base_url") or not payload.get("name"):
        abort(400, description="SET-003 Provider 名稱與 URL 不可空白")
    provider_id = current_app.extensions["inktime_provider_repository"].save(payload, g.user["id"])
    return {"id": provider_id}, 201


@bp.post("/api/v1/providers/<provider_id>/test")
@administrator_required
def test_provider(provider_id: str):
    config = current_app.extensions["inktime_provider_repository"].get(provider_id, include_secret=True)
    if config is None:
        abort(404)
    provider = OpenAICompatibleProvider(
        name=config["name"], base_url=config["base_url"], api_key=config.get("api_key", ""),
        timeout=min(15, config["timeout_seconds"]), supports_json_schema=bool(config["supports_json_schema"]),
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
