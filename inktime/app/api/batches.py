from __future__ import annotations

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, url_for

from inktime.app.core.json_values import (
    JsonScalarError,
    json_int,
    json_object_payload,
    nullable_json_float,
    reject_unknown_fields,
)
from inktime.app.web.access import administrator_required, login_required
from inktime.app.services.batch_analysis import BatchLifecycleError


bp = Blueprint("analysis_batches", __name__)


def _service():
    return current_app.extensions["inktime_batch_analysis_service"]


def _repository():
    return current_app.extensions["inktime_analysis_batch_repository"]


def _payload() -> dict:
    return json_object_payload(request, maximum_bytes=128 * 1024, error_prefix="BATCH-API-001")


def _photo_ids(payload: dict) -> list[str] | None:
    raw = payload.get("photo_ids")
    if raw is None:
        return None
    if type(raw) is not list or any(not isinstance(value, str) or not value for value in raw):
        raise ValueError("photo_ids 必須是非空字串陣列")
    return list(dict.fromkeys(raw))


def _parameters(payload: dict) -> dict:
    reject_unknown_fields(
        payload,
        {"scope", "sample_count", "photo_ids", "budget_limit"},
        error_prefix="BATCH-API-001",
    )
    scope = payload.get("scope", "sample")
    if type(scope) is not str or scope not in {
        "sample",
        "all_eligible_missing_analysis",
        "new_or_changed",
        "manual_selection",
    }:
        raise ValueError("不支援的 Batch scope")
    sample_count = json_int(
        payload,
        "sample_count",
        default=100,
        minimum=1,
        maximum=100_000,
        error_prefix="BATCH-API-001",
    )
    budget_limit = nullable_json_float(
        payload,
        "budget_limit",
        minimum=0,
        maximum=1_000_000,
        error_prefix="BATCH-API-001",
    )
    return {
        "scope": scope,
        "sample_count": sample_count,
        "photo_ids": _photo_ids(payload),
        "budget_limit": budget_limit,
    }


@bp.get("/analysis/batches")
@login_required
def batches_page():
    rows = _repository().list(limit=100)
    if str(g.user["role"]) != "administrator":
        rows = [
            row
            for row in rows
            if row.get("job_id")
            and current_app.extensions["inktime_job_repository"].can_access(
                str(row["job_id"]), str(g.user["id"]), administrator=False
            )
        ]
    return render_template("analysis_batches.html", batches=rows)


@bp.post("/analysis/batches/action")
@administrator_required
def batches_page_action():
    action = str(request.form.get("action", "estimate"))
    try:
        if action in {"estimate", "submit"}:
            payload = {
                "scope": request.form.get("scope", "sample"),
                "sample_count": int(request.form.get("sample_count", "100")),
            }
            if action == "estimate":
                result = _service().estimate(**_parameters(payload))
                return render_template(
                    "analysis_batches.html", batches=_repository().list(limit=100), estimate=result
                )
            _service().submit(created_by=str(g.user["id"]), **_parameters(payload))
        elif action == "cancel":
            _service().cancel(str(request.form["batch_id"]))
        elif action == "retry":
            _service().retry_failed(str(request.form["batch_id"]), created_by=str(g.user["id"]))
        elif action == "cleanup":
            _service().retry_cleanup(str(request.form["batch_id"]))
        else:
            abort(400, description="BATCH-API-002 不支援的操作")
    except (ValueError, KeyError, BatchLifecycleError) as exc:
        abort(400, description=f"BATCH-API-002 {exc}")
    return redirect(url_for("analysis_batches.batches_page"), code=303)


@bp.post("/api/v1/analysis/batches/estimate")
@administrator_required
def estimate_batch():
    try:
        return _service().estimate(**_parameters(_payload()))
    except (JsonScalarError, ValueError, BatchLifecycleError) as exc:
        abort(400, description=f"BATCH-API-001 {exc}")


@bp.post("/api/v1/analysis/batches")
@administrator_required
def create_batch():
    try:
        result = _service().submit(created_by=str(g.user["id"]), **_parameters(_payload()))
    except (JsonScalarError, ValueError, BatchLifecycleError) as exc:
        abort(409 if isinstance(exc, BatchLifecycleError) else 400, description=f"BATCH-API-003 {exc}")
    return result, 201


@bp.get("/api/v1/analysis/batches")
@login_required
def list_batches():
    rows = _repository().list(limit=100)
    if str(g.user["role"]) != "administrator":
        rows = [
            row
            for row in rows
            if row.get("job_id")
            and current_app.extensions["inktime_job_repository"].can_access(
                str(row["job_id"]), str(g.user["id"]), administrator=False
            )
        ]
    return {"batches": rows}


@bp.get("/api/v1/analysis/batches/<batch_id>")
@login_required
def batch_detail(batch_id: str):
    detail = _service().get_detail(batch_id)
    if detail is None:
        abort(404)
    if str(g.user["role"]) != "administrator" and (
        not detail.get("job_id")
        or not current_app.extensions["inktime_job_repository"].can_access(
            str(detail["job_id"]), str(g.user["id"]), administrator=False
        )
    ):
        abort(404)
    return detail


@bp.get("/analysis/batches/<batch_id>")
@login_required
def batch_detail_page(batch_id: str):
    detail = _service().get_detail(batch_id)
    if detail is None:
        abort(404)
    if str(g.user["role"]) != "administrator" and (
        not detail.get("job_id")
        or not current_app.extensions["inktime_job_repository"].can_access(
            str(detail["job_id"]), str(g.user["id"]), administrator=False
        )
    ):
        abort(404)
    return render_template("analysis_batch_detail.html", batch=detail)


@bp.post("/api/v1/analysis/batches/<batch_id>/cancel")
@administrator_required
def cancel_batch(batch_id: str):
    try:
        return _service().cancel(batch_id)
    except (KeyError, ValueError, BatchLifecycleError) as exc:
        abort(409, description=f"BATCH-API-004 {exc}")


@bp.post("/api/v1/analysis/batches/<batch_id>/retry-failed")
@administrator_required
def retry_failed(batch_id: str):
    try:
        return _service().retry_failed(batch_id, created_by=str(g.user["id"]))
    except (KeyError, ValueError, BatchLifecycleError) as exc:
        abort(409, description=f"BATCH-API-005 {exc}")


@bp.post("/api/v1/analysis/batches/<batch_id>/retry-cleanup")
@administrator_required
def retry_cleanup(batch_id: str):
    try:
        return {"job_id": _service().retry_cleanup(batch_id)}
    except (KeyError, ValueError, BatchLifecycleError) as exc:
        abort(409, description=f"BATCH-API-006 {exc}")
