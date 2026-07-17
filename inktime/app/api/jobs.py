from __future__ import annotations

from flask import Blueprint, Response, abort, current_app, g, render_template, request, stream_with_context
import json

from inktime.app.services.jobs import InvalidJobTransition, JobService
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("jobs", __name__)


def _service() -> JobService:
    return current_app.extensions["inktime_job_service"]


def _repository():
    return current_app.extensions["inktime_job_repository"]


@bp.get("/jobs")
@login_required
def jobs_page():
    return render_template("jobs.html", jobs=_repository().list())


@bp.get("/jobs/<job_id>")
@login_required
def job_detail(job_id: str):
    job = _repository().get(job_id)
    if job is None:
        abort(404)
    page = max(1, request.args.get("page", 1, type=int))
    return render_template(
        "job_detail.html",
        job=job,
        items=_repository().list_items(job_id, limit=100, offset=(page - 1) * 100),
        page=page,
    )


@bp.post("/api/v1/jobs")
@administrator_required
def create_job():
    payload = request.get_json(silent=True) or {}
    budget = payload.get("budget_limit")
    limit = payload.get("limit")
    job_id = _service().create_analysis_job(
        name=str(payload.get("name", "分析工作")),
        strategy=str(payload.get("strategy", "smart_two_stage")),
        settings=dict(payload.get("settings") or {}),
        created_by=g.user["id"],
        budget_limit=float(budget) if budget not in (None, "") else None,
        limit=max(1, min(int(limit), 100_000)) if limit not in (None, "") else None,
        photo_ids=payload.get("photo_ids"),
    )
    return {"id": job_id, "detail_url": f"/jobs/{job_id}"}, 201


@bp.post("/api/v1/jobs/<job_id>/<action>")
@administrator_required
def control_job(job_id: str, action: str):
    actions = {
        "start": _service().start,
        "pause": _service().pause,
        "resume": _service().resume,
        "cancel": _service().cancel,
        "retry-failed": _service().retry_failed,
    }
    function = actions.get(action)
    if function is None:
        abort(404)
    try:
        result = function(job_id)
    except InvalidJobTransition as exc:
        return {"error_code": exc.code, "message": str(exc)}, 409
    return {"status": "ok", "affected": result if isinstance(result, int) else None}


@bp.post("/api/v1/jobs/estimate")
@administrator_required
def estimate_job():
    payload = request.get_json(silent=True) or {}
    return _service().estimate(
        max(0, int(payload.get("photo_count", 0))),
        str(payload.get("strategy", "smart_two_stage")),
    )


@bp.get("/api/v1/jobs/<job_id>/export")
@login_required
def export_job(job_id: str):
    if _repository().get(job_id) is None:
        abort(404)

    def generate():
        yield '{"job_id":' + json.dumps(job_id) + ',"items":['
        offset = 0
        first = True
        while True:
            rows = _repository().list_items(job_id, limit=500, offset=offset)
            if not rows:
                break
            for row in rows:
                if not first:
                    yield ","
                first = False
                yield json.dumps(dict(row), ensure_ascii=False)
            offset += len(rows)
        yield "]}"

    return Response(
        stream_with_context(generate()),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="inktime-job-{job_id}.json"'},
    )
