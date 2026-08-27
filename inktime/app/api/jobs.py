from __future__ import annotations

import json

from flask import Blueprint, Response, abort, current_app, g, render_template, request, stream_with_context

from inktime.app.core.json_values import (
    JsonScalarError,
    json_bool,
    json_int,
    json_object_payload,
    nullable_json_float,
    optional_json_int,
)
from inktime.app.core.idempotency import request_fingerprint, scoped_idempotency_key
from inktime.app.domain.analysis.plan import canonical_json, fingerprint, normalize_analysis_strategy
from inktime.app.domain.analysis.execution_mode import execution_mode, permits_automatic_ai
from inktime.app.services.jobs import InvalidJobTransition, JobService
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("jobs", __name__)


def _service() -> JobService:
    return current_app.extensions["inktime_job_service"]


def _repository():
    return current_app.extensions["inktime_job_repository"]


def _payload() -> dict:
    return json_object_payload(request, maximum_bytes=256 * 1024, error_prefix="JOB-001")


def _analysis_plan(strategy: str) -> tuple[dict, str]:
    analysis = current_app.extensions["inktime_analysis_service"]
    settings = current_app.extensions["inktime_settings_repository"]
    scoring = dict(current_app.extensions["inktime_scoring_repository"].current())
    plan = analysis.build_plan(
        strategy=strategy,
        provider_route=(
            current_app.extensions["inktime_provider_service"].route_snapshot()
            if permits_automatic_ai(execution_mode(settings))
            else []
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


def _available_job_actions(job) -> list[str]:
    status = str(job["status"])
    actions: list[str] = []
    if status == "pending":
        actions.append("start")
    if status in {"running", "retrying"}:
        actions.append("pause")
    if status in {"paused", "budget_exceeded"}:
        actions.append("resume")
    if status not in {"completed", "completed_with_errors", "failed", "cancelled"}:
        actions.append("cancel")
    if status in {"failed", "completed_with_errors"} and int(job["failed_items"] or 0) > 0:
        actions.append("retry-failed")
    return actions


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
        available_actions=_available_job_actions(job),
        page=page,
    )


@bp.post("/api/v1/jobs")
@administrator_required
def create_job():
    payload = _payload()
    raw_settings = payload.get("settings", {})
    if type(raw_settings) is not dict:
        return {"message": "JOB-001 settings 必須是 JSON 物件"}, 400
    settings = dict(raw_settings)
    try:
        budget = nullable_json_float(
            payload,
            "budget_limit",
            minimum=0,
            maximum=1_000_000,
            error_prefix="JOB-001",
        )
        limit = optional_json_int(
            payload,
            "limit",
            minimum=1,
            maximum=100_000,
            error_prefix="JOB-001",
        )
        force_recompute = json_bool(
            payload,
            "force_recompute",
            default=str(payload.get("selection_mode", "pending")) == "force_all",
            error_prefix="JOB-001",
        )
    except JsonScalarError as exc:
        return {"message": str(exc)}, 400
    selection_mode = str(payload.get("selection_mode", "pending"))
    if selection_mode not in {"pending", "stale_only", "force_all"}:
        return {"message": "不支援的選片模式"}, 400
    if selection_mode == "force_all" and str(g.user["role"]) != "administrator":
        return {"message": "force_all 僅限管理員"}, 403
    try:
        strategy = normalize_analysis_strategy(payload.get("strategy", "single"))
    except ValueError as exc:
        return {"message": str(exc)}, 400
    if execution_mode(current_app.extensions["inktime_settings_repository"]) == "disabled":
        return {
            "error_code": "ANALYSIS-DISABLED",
            "message": "目前分析執行模式為完全停用，不會建立新的分析工作。",
        }, 409
    plan, _ = _analysis_plan(strategy)
    analysis_fingerprint = fingerprint(plan)
    idempotency_key = scoped_idempotency_key("analysis", str(g.user["id"]), request.headers.get("Idempotency-Key"))
    idempotency_fingerprint = (
        request_fingerprint(
            {
                "name": str(payload.get("name", "分析工作")),
                "strategy": strategy,
                "settings": settings,
                "budget_limit": budget,
                "limit": limit,
                "photo_ids": payload.get("photo_ids"),
                "selection_mode": selection_mode,
                "analysis_fingerprint": analysis_fingerprint,
                "force_recompute": force_recompute,
                "analysis_spec": plan,
            }
        )
        if idempotency_key
        else None
    )
    try:
        job_id = _service().create_analysis_job(
            name=str(payload.get("name", "分析工作")),
            strategy=strategy,
            settings=settings,
            created_by=g.user["id"],
            budget_limit=budget,
            limit=limit,
            photo_ids=payload.get("photo_ids"),
            selection_mode=selection_mode,
            analysis_fingerprint=analysis_fingerprint,
            force_recompute=force_recompute,
            analysis_spec=plan,
            dedupe_key=idempotency_key,
            request_fingerprint=idempotency_fingerprint,
        )
    except ValueError as exc:
        if str(exc) == "IDEMPOTENCY_CONFLICT":
            return {"error_code": "IDEMPOTENCY_CONFLICT", "message": str(exc)}, 409
        return {"message": str(exc)}, 409
    return {"id": job_id, "detail_url": f"/jobs/{job_id}"}, 201


@bp.post("/api/v1/jobs/selection-preview")
@administrator_required
def selection_preview():
    payload = _payload()
    mode = str(payload.get("selection_mode", "pending"))
    if mode not in {"pending", "stale_only", "force_all"}:
        return {"message": "不支援的選片模式"}, 400
    try:
        limit = optional_json_int(
            payload,
            "limit",
            minimum=1,
            maximum=100_000,
            error_prefix="JOB-001",
        )
    except JsonScalarError as exc:
        return {"message": str(exc)}, 400
    try:
        strategy = normalize_analysis_strategy(payload.get("strategy", "single"))
    except ValueError as exc:
        return {"message": str(exc)}, 400
    if execution_mode(current_app.extensions["inktime_settings_repository"]) == "disabled":
        return {
            "error_code": "ANALYSIS-DISABLED",
            "message": "目前分析執行模式為完全停用，不會建立新的分析工作。",
        }, 409
    _plan, _ = _analysis_plan(strategy)
    preview = _repository().selection_preview(
        analysis_fingerprint=fingerprint(_plan),
        selection_mode=mode,
        limit=limit,
    )
    estimate = _service().estimate(int(preview["limited_to"]), strategy)
    return {
        **preview,
        "image_calls": estimate["image_calls"],
        "estimated_stage_one": estimate["stage_one_photos"],
        "estimated_stage_two": estimate["stage_two_photos"],
        "estimated_cost": estimate["average_cost"],
    }


@bp.post("/api/v1/jobs/<job_id>/<action>")
@administrator_required
def control_job(job_id: str, action: str):
    _job_or_404(job_id)
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
        if action == "retry-failed":
            if not isinstance(result, int) or result < 1:
                raise InvalidJobTransition("沒有可重跑的失敗項目")
            _service().start(job_id)
    except InvalidJobTransition as exc:
        return {"error_code": exc.code, "message": str(exc)}, 409
    return {"status": "ok", "affected": result if isinstance(result, int) else None}


@bp.get("/api/v1/jobs/<job_id>")
@login_required
def job_status(job_id: str):
    job = _job_or_404(job_id)
    items = _repository().list_items(job_id, limit=100)
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
        "spent": float(job["spent"]),
        "available_actions": _available_job_actions(job),
        "items": [
            {
                "id": str(item["id"]),
                "photo_id": str(item["photo_id"]) if item["photo_id"] is not None else None,
                "status": str(item["status"]),
                "stage": str(item["stage"]),
                "attempts": int(item["attempts"]),
                "error_code": str(item["error_code"]) if item["error_code"] is not None else None,
            }
            for item in items
        ],
        "result": result,
        "error_code": error_code,
    }
    return response


@bp.post("/api/v1/jobs/estimate")
@administrator_required
def estimate_job():
    payload = _payload()
    try:
        photo_count = json_int(
            payload,
            "photo_count",
            default=0,
            minimum=0,
            maximum=1_000_000,
            error_prefix="JOB-001",
        )
    except JsonScalarError as exc:
        return {"message": str(exc)}, 400
    try:
        strategy = normalize_analysis_strategy(payload.get("strategy", "single"))
    except ValueError as exc:
        return {"message": str(exc)}, 400
    return _service().estimate(photo_count, strategy)


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
