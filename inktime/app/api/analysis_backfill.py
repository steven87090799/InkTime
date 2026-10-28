from __future__ import annotations

from flask import Blueprint, abort, current_app, g, render_template, request

from inktime.app.core.json_values import json_object_payload
from inktime.app.repositories.analysis_reservations import AnalysisReservationConflict
from inktime.app.web.access import administrator_required


bp = Blueprint("analysis_backfill", __name__)
STATUSES = {"", "completed", "missing", "queued", "running", "failed", "unknown", "excluded", "preprocessing"}


def _service():
    return current_app.extensions["inktime_analysis_backfill_service"]


def _state():
    category = request.args.get("status", "")
    page = request.args.get("page", 1, type=int)
    if category not in STATUSES or page is None or not 1 <= page <= 100000:
        abort(400, description="篩選或分頁不合法")
    service = _service()
    return {**service.repository.status(category=category, offset=(page - 1) * 50),
            "profile": service.profile(), "page": page, "category": category}


@bp.get("/analysis/missing")
@administrator_required
def page():
    return render_template("analysis_missing.html", state=_state())


@bp.get("/api/v1/analysis/missing")
@administrator_required
def status():
    return _state()


@bp.post("/api/v1/analysis/missing/profile")
@administrator_required
def configure():
    payload = json_object_payload(request, maximum_bytes=8192, error_prefix="BACKFILL-001")
    if set(payload) - {"model", "api_key", "concurrency", "rpm"}:
        abort(400, description="補齊設定包含不支援的欄位")
    try:
        _service().configure(payload, str(g.user["id"]))
    except ValueError as exc:
        return {"message": str(exc)}, 409
    return {"profile": _service().profile()}


@bp.post("/api/v1/analysis/missing/toggle")
@administrator_required
def toggle():
    payload = json_object_payload(request, maximum_bytes=1024, error_prefix="BACKFILL-001")
    if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
        abort(400, description="enabled 必須為 Boolean")
    try:
        state = _service().set_enabled(payload["enabled"], str(g.user["id"]))
    except ValueError as exc:
        return {"message": str(exc)}, 409
    # The scheduler/worker owns dispatch; an HTTP request never calls the model.
    return state


@bp.post("/api/v1/analysis/missing/jobs/<job_id>/retry-failed")
@administrator_required
def retry_failed(job_id: str):
    try:
        result = _service().retry_failed(job_id, str(g.user["id"]))
    except (ValueError, AnalysisReservationConflict) as exc:
        return {"message": str(exc)}, 409
    return {**result, "detail_url": f"/jobs/{result['job_id']}"}, 201 if result["created"] else 200
