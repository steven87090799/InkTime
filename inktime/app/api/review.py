"""Review workbench shell and bounded metadata/thumbnail APIs."""

from __future__ import annotations

from flask import Blueprint, abort, current_app, g, render_template, request, send_file

from inktime.app.core.json_values import json_int, json_object_payload
from inktime.app.core.paths import safe_join
from inktime.app.repositories.reviews import ReviewConflictError
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("review", __name__)


def _repository():
    return current_app.extensions["inktime_review_repository"]


def _filters() -> dict:
    boolean_keys = (
        "candidate_pool",
        "favorite",
        "low_confidence",
        "model_should_keep",
        "excluded",
        "understanding_incorrect",
        "caption_bad",
        "scores_unreasonable",
    )
    result = {
        "q": request.args.get("q", ""),
        "review_state": request.args.get("review_state", ""),
        "review_status": request.args.get("review_status", ""),
        "date": request.args.get("date", ""),
        "month_day": request.args.get("month_day", ""),
        "year": request.args.get("year", ""),
        "month": request.args.get("month", ""),
        "day": request.args.get("day", ""),
        "reason": request.args.get("reason", ""),
        "provider": request.args.get("provider", ""),
        "model": request.args.get("model", ""),
        "category": request.args.get("category", ""),
        "library_id": request.args.get("library_id", ""),
        "ai_status": request.args.get("ai_status", ""),
        "score_min": request.args.get("score_min", ""),
        "score_max": request.args.get("score_max", ""),
    }
    for key in boolean_keys:
        value = request.args.get(key)
        if value in {"0", "1"}:
            result[key] = value == "1"
        elif value not in {None, ""}:
            abort(400, description="REVIEW-001 布林篩選格式錯誤")
    return result


@bp.get("/review/photos")
@login_required
def review_page():
    return render_template("review_photos.html")


@bp.get("/api/review/photos")
@bp.get("/api/v1/review/photos")
@login_required
def review_photos():
    try:
        limit = max(1, min(request.args.get("limit", 40, type=int), 80))
        return _repository().list_photos(
            filters=_filters(), cursor=request.args.get("cursor"), limit=limit
        )
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.get("/api/review/summary")
@bp.get("/api/v1/review/summary")
@login_required
def review_summary():
    try:
        return _repository().summary(filters=_filters())
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.get("/api/review/date-facets")
@bp.get("/api/v1/review/date-facets")
@login_required
def review_date_facets():
    try:
        return _repository().date_facets(filters=_filters())
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.get("/api/review/photos/<photo_id>")
@bp.get("/api/v1/review/photos/<photo_id>")
@login_required
def review_photo(photo_id: str):
    photo = _repository().get(photo_id)
    if photo is None:
        abort(404, description="REVIEW-404 找不到照片")
    return photo


@bp.get("/api/review/photos/<photo_id>/thumbnail")
@bp.get("/api/v1/review/photos/<photo_id>/thumbnail")
@login_required
def review_thumbnail(photo_id: str):
    if _repository().get(photo_id) is None:
        abort(404, description="REVIEW-404 找不到照片縮圖")
    photo = current_app.extensions["inktime_photo_repository"].get_with_path(photo_id)
    if photo is None or not str(photo["sha256"] or ""):
        abort(404, description="REVIEW-404 找不到照片縮圖")
    source = safe_join(photo["root_path"], str(photo["relative_path"]))
    if not source.is_file():
        abort(404, description="REVIEW-404 找不到照片縮圖")
    try:
        thumbnail = current_app.extensions["inktime_thumbnail_cache"].get_or_create(
            source, str(photo["sha256"]), 512
        )
    except (OSError, ValueError):
        abort(422, description="REVIEW-422 縮圖建立失敗")
    return send_file(thumbnail, mimetype="image/jpeg", max_age=300, conditional=True)


@bp.patch("/api/review/photos/<photo_id>")
@bp.patch("/api/v1/review/photos/<photo_id>")
@bp.patch("/api/review/photos/<photo_id>/review")
@bp.patch("/api/review/photos/<photo_id>/caption")
@administrator_required
def update_review(photo_id: str):
    try:
        payload = json_object_payload(request, maximum_bytes=64 * 1024, error_prefix="REVIEW-001")
        expected_version = json_int(
            payload, "expected_version", required=True, minimum=0, maximum=2_147_483_647, error_prefix="REVIEW-001"
        )
        payload = dict(payload)
        payload.pop("expected_version", None)
        result = _repository().update(
            photo_id, payload, actor_id=str(g.user["id"]), expected_version=expected_version
        )
    except ReviewConflictError as exc:
        return {"error_code": exc.code, "message": str(exc), "current": exc.current}, 409
    except KeyError:
        abort(404, description="REVIEW-404 找不到照片")
    except (TypeError, ValueError) as exc:
        abort(400, description=f"REVIEW-001 {exc}")
    return {"status": "ok", "photo": result}
