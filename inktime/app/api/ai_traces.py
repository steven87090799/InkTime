from __future__ import annotations

from flask import Blueprint, abort, current_app, g, render_template, request

from inktime.app.web.access import login_required


bp = Blueprint("ai_traces", __name__)


def _repository():
    return current_app.extensions["inktime_ai_trace_repository"]


def _cursor(name: str) -> int | None:
    value = request.args.get(name)
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        abort(400, description="AI-TRACE-001 cursor 格式錯誤")
    if parsed < 0:
        abort(400, description="AI-TRACE-001 cursor 格式錯誤")
    return parsed


def _filters() -> dict[str, str]:
    return {
        key: request.args.get(key, "").strip()[:160]
        for key in ("status", "provider", "model", "job_id", "photo_id", "stage")
    }


@bp.get("/ai/traces")
@login_required
def traces_page():
    return render_template("ai_traces.html", poll_seconds=5)


@bp.get("/ai/traces/<trace_id>")
@login_required
def trace_detail_page(trace_id: str):
    trace = _repository().detail(trace_id, include_payloads=False)
    if trace is None:
        abort(404)
    return render_template("ai_trace_detail.html", trace=trace)


@bp.get("/api/v1/ai/traces")
@login_required
def trace_list():
    try:
        limit = int(request.args.get("limit", "50"))
    except (TypeError, ValueError):
        abort(400, description="AI-TRACE-001 limit 格式錯誤")
    rows = _repository().list(
        filters=_filters(),
        limit=limit,
        before=_cursor("before"),
        after=_cursor("after"),
    )
    for row in rows:
        row["detail_url"] = f"/ai/traces/{row['trace_id']}"
        row["photo_url"] = f"/photos/{row['photo_id']}"
        row["image_url"] = f"/api/v1/photos/{row['photo_id']}/image"
        row["job_url"] = f"/jobs/{row['job_id']}" if row.get("job_id") else None
        row["retry_count"] = max(0, int(row.get("attempt_count") or 0) - 1)
    return {
        "traces": rows,
        "highest_id": max((int(row["id"]) for row in rows), default=None),
        "next_before": min((int(row["id"]) for row in rows), default=None),
        "limit": max(1, min(int(limit), 100)),
    }


@bp.get("/api/v1/ai/traces/<trace_id>")
@login_required
def trace_detail(trace_id: str):
    include_payloads = str(g.user["role"]) == "administrator"
    trace = _repository().detail(trace_id, include_payloads=include_payloads)
    if trace is None:
        abort(404)
    trace["photo_url"] = f"/photos/{trace['photo_id']}"
    trace["image_url"] = f"/api/v1/photos/{trace['photo_id']}/image"
    trace["job_url"] = f"/jobs/{trace['job_id']}" if trace.get("job_id") else None
    return {"trace": trace}
