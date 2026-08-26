from __future__ import annotations

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, url_for

from inktime.app.core.json_values import (
    JsonScalarError,
    json_bool,
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


def _estimate_parameters(payload: dict) -> dict:
    parameters = _parameters(payload)
    parameters.pop("budget_limit", None)
    return parameters


@bp.get("/analysis/batches")
@login_required
def batches_page():
    rows = _repository().list(limit=100)
    holds = _repository().list_operator_holds(limit=100)
    if str(g.user["role"]) != "administrator":
        rows = [
            row
            for row in rows
            if row.get("job_id")
            and current_app.extensions["inktime_job_repository"].can_access(
                str(row["job_id"]), str(g.user["id"]), administrator=False
            )
        ]
        holds = [
            row
            for row in holds
            if row.get("job_id")
            and current_app.extensions["inktime_job_repository"].can_access(
                str(row["job_id"]), str(g.user["id"]), administrator=False
            )
        ]
    return render_template("analysis_batches.html", batches=rows, operator_holds=holds)


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
                result = _service().estimate(**_estimate_parameters(payload))
                return render_template(
                    "analysis_batches.html",
                    batches=_repository().list(limit=100),
                    operator_holds=_repository().list_operator_holds(limit=100),
                    estimate=result,
                )
            _service().submit(created_by=str(g.user["id"]), **_parameters(payload))
        elif action == "cancel":
            _service().cancel(str(request.form["batch_id"]))
        elif action == "retry":
            _service().retry_failed(str(request.form["batch_id"]), created_by=str(g.user["id"]))
        elif action == "cleanup":
            _service().retry_cleanup(str(request.form["batch_id"]))
        elif action == "recover":
            _service().recover_submission(str(request.form["batch_id"]), str(request.form["remote_batch_id"]))
        elif action == "recover_upload":
            _service().recover_uploaded_file(
                str(request.form["batch_id"]), str(request.form["remote_file_id"])
            )
        elif action == "abandon":
            if request.form.get("confirm") != "true":
                raise ValueError("Abandon 必須明確確認遠端 Batch 不存在")
            _service().abandon(
                str(request.form["batch_id"]),
                confirmed_no_remote=True,
                remote_file_id=request.form.get("remote_file_id") or None,
                confirmed_remote_file_deleted=request.form.get("confirmed_remote_file_deleted") == "true",
            )
        else:
            abort(400, description="BATCH-API-002 不支援的操作")
    except (ValueError, KeyError, BatchLifecycleError) as exc:
        abort(400, description=f"BATCH-API-002 {exc}")
    return redirect(url_for("analysis_batches.batches_page"), code=303)


@bp.post("/api/v1/analysis/batches/estimate")
@administrator_required
def estimate_batch():
    try:
        return _service().estimate(**_estimate_parameters(_payload()))
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
        payload = _payload()
        reject_unknown_fields(payload, set(), error_prefix="BATCH-API-004")
        return _service().cancel(batch_id)
    except (JsonScalarError, KeyError, ValueError, BatchLifecycleError) as exc:
        abort(409, description=f"BATCH-API-004 {exc}")


@bp.post("/api/v1/analysis/batches/<batch_id>/retry-failed")
@administrator_required
def retry_failed(batch_id: str):
    try:
        payload = _payload()
        reject_unknown_fields(payload, set(), error_prefix="BATCH-API-005")
        return _service().retry_failed(batch_id, created_by=str(g.user["id"]))
    except (JsonScalarError, KeyError, ValueError, BatchLifecycleError) as exc:
        abort(409, description=f"BATCH-API-005 {exc}")


@bp.post("/api/v1/analysis/batches/<batch_id>/retry-cleanup")
@administrator_required
def retry_cleanup(batch_id: str):
    try:
        payload = _payload()
        reject_unknown_fields(payload, set(), error_prefix="BATCH-API-006")
        result = _service().retry_cleanup(batch_id)
        return result
    except (JsonScalarError, KeyError, ValueError, BatchLifecycleError) as exc:
        abort(409, description=f"BATCH-API-006 {exc}")


@bp.post("/api/v1/analysis/batches/<batch_id>/recover-submission")
@administrator_required
def recover_submission(batch_id: str):
    try:
        payload = _payload()
        reject_unknown_fields(payload, {"remote_batch_id"}, error_prefix="BATCH-API-007")
        remote_batch_id = payload.get("remote_batch_id")
        if type(remote_batch_id) is not str or not remote_batch_id.strip():
            raise ValueError("remote_batch_id 必須是非空字串")
        return _service().recover_submission(batch_id, remote_batch_id)
    except JsonScalarError as exc:
        abort(400, description=f"BATCH-API-007 {exc}")
    except BatchLifecycleError as exc:
        return {"error_code": exc.code, "message": str(exc)}, 409
    except (ValueError, KeyError) as exc:
        abort(409, description=f"BATCH-API-007 {exc}")


@bp.post("/api/v1/analysis/batches/<batch_id>/recover-upload")
@administrator_required
def recover_upload(batch_id: str):
    try:
        payload = _payload()
        reject_unknown_fields(payload, {"remote_file_id"}, error_prefix="BATCH-API-009")
        remote_file_id = payload.get("remote_file_id")
        if type(remote_file_id) is not str or not remote_file_id.strip():
            raise ValueError("remote_file_id 必須是非空字串")
        return _service().recover_uploaded_file(batch_id, remote_file_id)
    except JsonScalarError as exc:
        abort(400, description=f"BATCH-API-009 {exc}")
    except BatchLifecycleError as exc:
        return {"error_code": exc.code, "message": str(exc)}, 409
    except (ValueError, KeyError) as exc:
        abort(409, description=f"BATCH-API-009 {exc}")


@bp.post("/api/v1/analysis/batches/<batch_id>/abandon")
@administrator_required
def abandon_batch(batch_id: str):
    try:
        payload = _payload()
        reject_unknown_fields(
            payload,
            {"confirm", "remote_file_id", "confirmed_remote_file_deleted"},
            error_prefix="BATCH-API-008",
        )
        confirmed = json_bool(payload, "confirm", default=False, error_prefix="BATCH-API-008")
        confirmed_deleted = json_bool(
            payload, "confirmed_remote_file_deleted", default=False, error_prefix="BATCH-API-008"
        )
        remote_file_id = payload.get("remote_file_id")
        if remote_file_id is not None and type(remote_file_id) is not str:
            raise ValueError("remote_file_id 必須是字串")
        return _service().abandon(
            batch_id,
            confirmed_no_remote=confirmed,
            remote_file_id=remote_file_id,
            confirmed_remote_file_deleted=confirmed_deleted,
        )
    except JsonScalarError as exc:
        abort(400, description=f"BATCH-API-008 {exc}")
    except BatchLifecycleError as exc:
        return {"error_code": exc.code, "message": str(exc)}, 409
    except (ValueError, KeyError) as exc:
        abort(409, description=f"BATCH-API-008 {exc}")
