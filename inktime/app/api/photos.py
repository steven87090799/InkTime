from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, g, render_template, request, send_file

from inktime.app.core.paths import safe_join
from inktime.app.domain.analysis.schema import ALLOWED_TYPES
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("photos", __name__)


def _repository():
    return current_app.extensions["inktime_photo_repository"]


@bp.get("/photos")
@login_required
def photos_page():
    page = max(1, request.args.get("page", 1, type=int))
    rows, total = _repository().search(
        query=request.args.get("q", "").strip(),
        status=request.args.get("status", "").strip(),
        photo_type=request.args.get("type", "").strip(),
        minimum_score=request.args.get("score", type=float),
        duplicate_only=request.args.get("duplicates") == "1",
        limit=60,
        offset=(page - 1) * 60,
    )
    return render_template("photos.html", photos=rows, total=total, page=page)


@bp.get("/photos/<photo_id>")
@login_required
def photo_detail(photo_id: str):
    photo = _repository().get_with_path(photo_id)
    if photo is None:
        abort(404)
    location_name = current_app.extensions["inktime_location_resolver"].resolve(
        photo["gps_lat"],
        photo["gps_lon"],
        max_distance_km=float(
            current_app.extensions["inktime_settings_repository"].get(
                "render.location_max_distance_km", 80
            )
        ),
    )
    with current_app.extensions["inktime_database"].session() as connection:
        analyses = connection.execute(
            """
            SELECT a.*,v.name AS scoring_version_name
            FROM photo_analysis a
            LEFT JOIN scoring_rule_versions v ON v.id=a.scoring_version_id
            WHERE a.photo_id=? ORDER BY a.created_at DESC
            """,
            (photo_id,),
        ).fetchall()
        usage = connection.execute(
            "SELECT * FROM api_usage WHERE photo_id=? ORDER BY started_at DESC", (photo_id,)
        ).fetchall()
        errors = connection.execute(
            "SELECT * FROM job_errors WHERE photo_id=? ORDER BY last_seen_at DESC", (photo_id,)
        ).fetchall()
        events = connection.execute(
            "SELECT * FROM photo_events WHERE photo_id=? ORDER BY created_at DESC LIMIT 100", (photo_id,)
        ).fetchall()
    return render_template(
        "photo_detail.html",
        photo=photo,
        analyses=analyses,
        usage=usage,
        errors=errors,
        events=events,
        allowed_types=sorted(ALLOWED_TYPES),
        location_name=location_name,
    )


@bp.patch("/api/v1/photos/<photo_id>")
@administrator_required
def update_photo(photo_id: str):
    payload = request.get_json(silent=True) or {}
    types = [str(value) for value in payload.get("types", [])]
    if not types or len(types) != len(set(types)) or any(value not in ALLOWED_TYPES for value in types):
        abort(400, description="IMG-004 照片類型不合法")
    side_caption = str(payload.get("side_caption", "")).strip()
    if len(side_caption) > 120:
        abort(400, description="IMG-004 電子紙短文案不可超過 120 字")
    captured_at = str(payload.get("captured_at", "")).strip() or None
    try:
        _repository().update_manual(
            photo_id,
            favorite=bool(payload.get("favorite", False)),
            captured_at=captured_at,
            types=types,
            side_caption=side_caption,
            changed_by=str(g.user["id"]),
        )
    except KeyError:
        abort(404)
    return {"status": "ok"}


@bp.get("/api/v1/photos/<photo_id>/image")
@login_required
def photo_image(photo_id: str):
    photo = _repository().get_with_path(photo_id)
    if photo is None:
        abort(404)
    path = safe_join(Path(photo["root_path"]), photo["relative_path"])
    if not path.is_file():
        abort(404)
    return send_file(path, conditional=True, max_age=300)


@bp.post("/api/v1/cache/clear")
@administrator_required
def clear_cache():
    removed = current_app.extensions["inktime_thumbnail_cache"].clear()
    return {"removed": removed}
