from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, render_template, request, send_file

from inktime.app.core.paths import safe_join
from inktime.app.web.access import login_required


bp = Blueprint("photos", __name__)


def _repository():
    return current_app.extensions["inktime_photo_repository"]


@bp.get("/photos")
@login_required
def photos_page():
    page = max(1, request.args.get("page", 1, type=int))
    rows, total = _repository().search(
        query=request.args.get("q", "").strip(), status=request.args.get("status", "").strip(),
        photo_type=request.args.get("type", "").strip(), minimum_score=request.args.get("score", type=float),
        duplicate_only=request.args.get("duplicates") == "1", limit=60, offset=(page - 1) * 60,
    )
    return render_template("photos.html", photos=rows, total=total, page=page)


@bp.get("/photos/<photo_id>")
@login_required
def photo_detail(photo_id: str):
    photo = _repository().get_with_path(photo_id)
    if photo is None:
        abort(404)
    with current_app.extensions["inktime_database"].session() as connection:
        analyses = connection.execute("SELECT * FROM photo_analysis WHERE photo_id=? ORDER BY created_at DESC", (photo_id,)).fetchall()
        usage = connection.execute("SELECT * FROM api_usage WHERE photo_id=? ORDER BY started_at DESC", (photo_id,)).fetchall()
        errors = connection.execute("SELECT * FROM job_errors WHERE photo_id=? ORDER BY last_seen_at DESC", (photo_id,)).fetchall()
    return render_template("photo_detail.html", photo=photo, analyses=analyses, usage=usage, errors=errors)


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
def clear_cache():
    from inktime.app.web.access import administrator_required
    # 裝飾器無法在函式內套用；before_request 已驗證登入，這裡明確檢查角色。
    from flask import g
    if g.user["role"] != "administrator":
        abort(403, description="AUTH-004 權限不足")
    removed = current_app.extensions["inktime_thumbnail_cache"].clear()
    return {"removed": removed}
