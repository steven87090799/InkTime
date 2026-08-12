from __future__ import annotations

from flask import Blueprint, abort, current_app, g, render_template, request

from inktime.app.web.access import login_required


bp = Blueprint("ai_traces", __name__)


def _repository():
    return current_app.extensions["inktime_ai_trace_repository"]


def _filters() -> dict[str, str]:
    return {
        name: request.args.get(name, "").strip()[:160]
        for name in ("status", "provider", "model", "job_id", "photo_id", "stage", "trace_id")
    }


@bp.get("/ai/traces")
@login_required
def trace_list_page():
    return render_template("ai_traces.html")


@bp.get("/ai/traces/<trace_id>")
@login_required
def trace_detail_page(trace_id: str):
    detail = _repository().detail(
        trace_id,
        include_sensitive=str(g.user["role"]) == "administrator",
    )
    if detail is None:
        abort(404)
    return render_template(
        "ai_trace_detail.html",
        trace=detail,
        administrator=str(g.user["role"]) == "administrator",
    )


@bp.get("/api/v1/ai/traces")
@login_required
def trace_list_api():
    limit = request.args.get("limit", 50, type=int)
    if limit is None:
        abort(400, description="AI-TRACE-001 limit 格式錯誤")
    before_id = request.args.get("before_id", type=int)
    return _repository().list_runs(filters=_filters(), limit=limit, before_id=before_id)


@bp.get("/api/v1/ai/traces/<trace_id>")
@login_required
def trace_detail_api(trace_id: str):
    detail = _repository().detail(
        trace_id,
        include_sensitive=str(g.user["role"]) == "administrator",
    )
    if detail is None:
        abort(404, description="AI-TRACE-404 找不到 AI Trace")
    return detail
