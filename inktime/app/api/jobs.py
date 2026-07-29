from __future__ import annotations

import json

from flask import Blueprint, Response, abort, current_app, g, render_template, request, stream_with_context

from inktime.app.core.json_values import json_bool
from inktime.app.domain.analysis.plan import canonical_json, fingerprint
from inktime.app.domain.analysis.execution_mode import execution_mode, permits_automatic_ai
from inktime.app.services.jobs import InvalidJobTransition, JobService
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("jobs", __name__)


def _service() -> JobService:
    return current_app.extensions["inktime_job_service"]


def _repository():
    return current_app.extensions["inktime_job_repository"]


def _analysis_plan(strategy: str) -> tuple[dict, str]:
    analysis = current_app.extensions["inktime_analysis_service"]
    settings = current_app.extensions["inktime_settings_repository"]
    scoring = dict(current_app.extensions["inktime_scoring_repository"].current())
    plan = analysis.build_plan(
        strategy=strategy,
        provider_route=(
            current_app.extensions["inktime_provider_service"].route_snapshot()
            if permits_automatic_ai(execution_mode(settings)) else []
        ),
        scoring_profile=scoring,
    )
    return plan, canonical_json(plan)


def _job_or_404(job_id: str):
    repository = _repository()
    job = repository.get(job_id)
    if job is None or not repository.can_access(
        job_id,
        str(g.user["id"]),
        administrator=str(g.user["role"]) == "administrator",
    ):
        abort(404)
    return job


@bp.get("/jobs")
@login_required
def jobs_page():
    jobs = (
        _repository().list()
        if str(g.user["role"]) == "administrator"
        else _repository().list_for_user(str(g.user["id"]))
    )
    return render_template("jobs.html", jobs=jobs)


@bp.get("/jobs/<job_id>")
@login_required
def job_detail(job_id: str):
    job = _job_or_404(job_id)
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
    settings = dict(payload.get("settings") or {})
    selection_mode = str(payload.get("selection_mode", "pending"))
    if selection_mode not in {"pending", "stale_only", "force_all"}:
        return {"message": "不支援的選片模式"}, 400
    if selection_mode == "force_all" and str(g.user["role"]) != "administrator":
        return {"message": "force_all 僅限管理員"}, 403
    strategy = str(payload.get("strategy", "smart_two_stage"))
    if execution_mode(current_app.extensions["inktime_settings_repository"]) == "disabled":
        return {
            "error_code": "ANALYSIS-DISABLED",
            "message": "目前分析執行模式為完全停用，不會建立新的分析工作。",
        }, 409
    plan, _ = _analysis_plan(strategy)
    analysis_fingerprint = fingerprint(plan)
    try:
        force_recompute = json_bool(
            payload,
            "force_recompute",
            default=selection_mode == "force_all",
            error_prefix="JOB-001",
        )
        job_id = _service().create_analysis_job(
            name=str(payload.get("name", "分析工作")),
            strategy=strategy,
            settings=settings,
            created_by=g.user["id"],
            budget_limit=float(budget) if budget not in (None, "") else None,
            limit=max(1, min(int(limit), 100_000)) if limit not in (None, "") else None,
            photo_ids=payload.get("photo_ids"),
            selection_mode=selection_mode,
            analysis_fingerprint=analysis_fingerprint,
            force_recompute=force_recompute,
            analysis_spec=plan,
        )
    except ValueError as exc:
        return {"message": str(exc)}, 409
    return {"id": job_id, "detail_url": f"/jobs/{job_id}"}, 201


@bp.post("/api/v1/jobs/selection-preview")
@administrator_required
def selection_preview():
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("selection_mode", "pending"))
    if mode not in {"pending", "stale_only", "force_all"}:
        return {"message": "不支援的選片模式"}, 400
    limit = payload.get("limit")
    strategy = str(payload.get("strategy", "smart_two_stage"))
    if execution_mode(current_app.extensions["inktime_settings_repository"]) == "disabled":
        return {
            "error_code": "ANALYSIS-DISABLED",
            "message": "目前分析執行模式為完全停用，不會建立新的分析工作。",
        }, 409
    _plan, _ = _analysis_plan(strategy)
    preview = _repository().selection_preview(
        analysis_fingerprint=fingerprint(_plan),
        selection_mode=mode,
        limit=max(1, min(int(limit), 100_000)) if limit not in (None, "") else None,
    )
    estimate = _service().estimate(int(preview["limited_to"]), strategy)
    return {
        **preview,
        "estimated_stage_one": estimate["stage_one_photos"],
        "estimated_stage_two": estimate["stage_two_photos"],
        "estimated_cost": estimate["average_cost"],
    }


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


@bp.get("/api/v1/jobs/<job_id>")
@login_required
def job_status(job_id: str):
    job = _job_or_404(job_id)
    items = _repository().list_items(job_id, limit=1)
    result = None
    error_code = None
    if items:
        error_code = items[0]["error_code"]
        if items[0]["result_json"]:
            try:
                result = json.loads(str(items[0]["result_json"]))
            except json.JSONDecodeError:
                result = None
    response = {
        "id": str(job["id"]),
        "kind": str(job["kind"]),
        "status": str(job["status"]),
        "completed_items": int(job["completed_items"]),
        "failed_items": int(job["failed_items"]),
        "total_items": int(job["total_items"]),
        "result": result,
        "error_code": error_code,
    }
    return response


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
    _job_or_404(job_id)

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
