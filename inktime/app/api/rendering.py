from __future__ import annotations

from pathlib import Path
import tempfile

from flask import Blueprint, abort, current_app, g, render_template, request, send_file

from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("rendering", __name__)


@bp.get("/rendering")
@login_required
def rendering_page():
    return render_template(
        "rendering.html",
        releases=current_app.extensions["inktime_release_publisher"].list(),
        fonts=current_app.extensions["inktime_font_manager"].scan(),
    )


@bp.get("/api/v1/rendering/preview/<photo_id>")
@login_required
def preview(photo_id: str):
    from io import BytesIO

    try:
        image = current_app.extensions["inktime_render_service"].render_photo(photo_id)
    except KeyError:
        abort(404)
    output = BytesIO()
    image.save(output, "PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png")


@bp.post("/api/v1/releases")
@administrator_required
def publish_release():
    payload = request.get_json(silent=True) or {}
    repository = current_app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(
        kind="render",
        name="電子紙正式發布",
        settings={"photo_ids": [str(value) for value in payload.get("photo_ids", [])]},
        created_by=g.user["id"],
    )
    current_app.extensions["inktime_job_service"].start(job_id)
    return {"id": job_id, "detail_url": f"/jobs/{job_id}"}, 202


@bp.post("/api/v1/releases/<release_id>/rollback")
@administrator_required
def rollback_release(release_id: str):
    try:
        current_app.extensions["inktime_render_service"].rollback(release_id)
    except KeyError:
        abort(404)
    return {"status": "ok"}


@bp.post("/api/v1/fonts")
@administrator_required
def upload_font():
    uploaded = request.files.get("font")
    if uploaded is None or not uploaded.filename:
        abort(400, description="IMG-002 請選擇字型檔案")
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in {".ttf", ".otf", ".ttc"}:
        abort(400, description="IMG-002 只支援 TTF、OTF 或 TTC")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        uploaded.save(temporary)
        temporary_path = Path(temporary.name)
    try:
        destination = current_app.extensions["inktime_font_manager"].install(temporary_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    current_app.extensions["inktime_settings_repository"].update(
        "render.font_path",
        str(destination),
        changed_by=g.user["id"],
        source_ip=request.remote_addr or "unknown",
    )
    return {"name": destination.name}, 201
