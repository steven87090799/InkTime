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
    with current_app.extensions["inktime_database"].session() as connection:
        summary = connection.execute(
            """
            SELECT COUNT(*) trace_count,COUNT(DISTINCT photo_id) photo_count,
                   COUNT(DISTINCT job_id) job_count,
                   COALESCE(SUM(COALESCE(LENGTH(final_result_json),0)+COALESCE(LENGTH(error_message),0)),0)
                       AS run_payload_bytes
            FROM ai_trace_runs
            """
        ).fetchone()
        attempts = connection.execute(
            """
            SELECT COUNT(*) attempt_count,
                   COALESCE(SUM(
                       COALESCE(LENGTH(request_json_sanitized),0)+
                       COALESCE(LENGTH(response_raw_sanitized),0)+
                       COALESCE(LENGTH(response_parsed_json),0)+
                       COALESCE(LENGTH(error_message),0)
                   ),0) AS attempt_payload_bytes
            FROM ai_trace_attempts
            """
        ).fetchone()
        retention = connection.execute(
            "SELECT enabled,retention_days,cleanup_batch_size,last_run_at "
            "FROM data_retention_policies WHERE data_type='ai_trace'"
        ).fetchone()
    trace_summary = {**dict(summary), **dict(attempts)}
    # SUM(LENGTH(...)) with COALESCE already returns integers, including for an
    # empty database. These are database aggregates, not request JSON scalars.
    payload_bytes = trace_summary["run_payload_bytes"] + trace_summary["attempt_payload_bytes"]
    trace_count = int(trace_summary["trace_count"] or 0)
    average_bytes = payload_bytes / trace_count if trace_count else 0
    trace_summary.update(
        {
            "payload_mib": round(payload_bytes / 1_048_576, 2),
            "estimated_100k_gib": round(average_bytes * 100_000 / 1_073_741_824, 2),
            "retention": dict(retention) if retention is not None else None,
        }
    )
    return render_template("ai_traces.html", trace_summary=trace_summary)


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
