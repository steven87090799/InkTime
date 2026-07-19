from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
import time

from flask import Blueprint, abort, current_app, g, render_template, request, send_file
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from inktime.app.web.access import administrator_required, login_required
from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.domain.rendering import (
    DISPLAY_PROFILES,
    DITHER_ALGORITHMS,
    FONT_COMPATIBILITY_TEXT,
    FONT_PREVIEW_TEXT,
    FontCoverageError,
    encode_image,
    profile_summaries,
)


bp = Blueprint("rendering", __name__)
SIMULATOR_CANVAS_SIZE = (480, 800)
SIMULATOR_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
MAX_SIMULATOR_PHOTO_BYTES = 25 * 1024 * 1024
MAX_SIMULATOR_PHOTO_PIXELS = 40_000_000
MAX_FONT_BYTES = 64 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


@bp.get("/rendering")
@login_required
def rendering_page():
    settings = current_app.extensions["inktime_settings_repository"]
    current_font_reference = str(settings.get("render.font_path", ""))
    fonts = current_app.extensions["inktime_font_manager"].options(current_font_reference)
    return render_template(
        "rendering.html",
        releases=current_app.extensions["inktime_release_publisher"].list(),
        fonts=fonts,
        current_font=next((font for font in fonts if font.active), None),
        profiles=profile_summaries(),
        current_profile=str(settings.get("render.profile", "safe_4c")),
        current_dither=str(settings.get("render.dither", "floyd_steinberg")),
        dither_strength=float(settings.get("render.dither_strength", 1.0)),
        color_distance=str(settings.get("render.color_distance", "oklab")),
    )


@bp.get("/simulator")
@login_required
def simulator_page():
    settings = current_app.extensions["inktime_settings_repository"]
    return render_template(
        "simulator.html",
        profiles=profile_summaries(),
        current_profile=str(settings.get("render.profile", "safe_4c")),
        current_dither=str(settings.get("render.dither", "floyd_steinberg")),
        dither_strength=float(settings.get("render.dither_strength", 1.0)),
        color_distance=str(settings.get("render.color_distance", "oklab")),
    )


@bp.post("/api/v1/rendering/simulate")
@login_required
def simulate():
    uploaded = request.files.get("photo")
    if uploaded is None or not uploaded.filename:
        abort(400, description="IMG-002 請選擇模擬照片")
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in SIMULATOR_IMAGE_SUFFIXES:
        abort(400, description="IMG-002 模擬照片格式不支援")

    profile_key = str(request.form.get("profile", "safe_4c"))
    dither = str(request.form.get("dither", "floyd_steinberg"))
    color_distance = str(request.form.get("color_distance", "oklab"))
    fit = str(request.form.get("fit", "cover"))
    try:
        strength = float(request.form.get("strength", "1"))
    except (TypeError, ValueError):
        abort(400, description="RENDER-004 抖動強度格式不合法")
    if profile_key not in DISPLAY_PROFILES or dither not in DITHER_ALGORITHMS:
        abort(400, description="RENDER-004 模擬 Profile 或抖動算法不合法")
    if fit not in {"cover", "contain"}:
        abort(400, description="RENDER-004 圖片縮放模式不合法")

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="inktime-simulator-") as directory:
        source_path = Path(directory) / f"source{suffix}"
        size = 0
        with source_path.open("wb") as destination:
            while chunk := uploaded.stream.read(UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_SIMULATOR_PHOTO_BYTES:
                    abort(413, description="IMG-002 模擬照片不可超過 25 MiB")
                destination.write(chunk)
        try:
            if suffix in {".heic", ".heif"}:
                from pillow_heif import register_heif_opener

                register_heif_opener()
            with Image.open(source_path) as opened:
                if opened.width * opened.height > MAX_SIMULATOR_PHOTO_PIXELS:
                    abort(413, description="IMG-002 模擬照片像素不可超過 4000 萬")
                opened.load()
                source_size = f"{opened.width}x{opened.height}"
                image = ImageOps.exif_transpose(opened).convert("RGB")
            if fit == "cover":
                canvas = ImageOps.fit(
                    image, SIMULATOR_CANVAS_SIZE, method=Image.Resampling.LANCZOS
                )
            else:
                fitted = ImageOps.contain(
                    image, SIMULATOR_CANVAS_SIZE, method=Image.Resampling.LANCZOS
                )
                canvas = Image.new("RGB", SIMULATOR_CANVAS_SIZE, "white")
                canvas.paste(
                    fitted,
                    ((canvas.width - fitted.width) // 2, (canvas.height - fitted.height) // 2),
                )
            encoded = encode_image(
                canvas,
                profile_key=profile_key,
                dither=dither,
                color_distance=color_distance,
                strength=strength,
            )
        except (UnidentifiedImageError, OSError):
            abort(400, description="IMG-002 無法解碼模擬照片")
        except ValueError as exc:
            description = str(exc)
            abort(
                400,
                description=(
                    description if "-" in description[:12] else f"RENDER-004 {description}"
                ),
            )

    output = BytesIO()
    encoded.preview.save(output, "PNG", optimize=True)
    output.seek(0)
    response = send_file(output, mimetype="image/png", max_age=0)
    response.headers["X-InkTime-Profile"] = profile_key
    response.headers["X-InkTime-Dither"] = dither
    response.headers["X-InkTime-Canvas"] = "480x800"
    response.headers["X-InkTime-Source"] = source_size
    response.headers["X-InkTime-Payload-Bytes"] = str(len(encoded.payload))
    response.headers["X-InkTime-Render-Ms"] = str(int((time.perf_counter() - started) * 1000))
    response.headers["X-InkTime-Model"] = "disabled"
    return response


@bp.get("/api/v1/rendering/preview/<photo_id>")
@login_required
def preview(photo_id: str):
    try:
        image = current_app.extensions["inktime_render_service"].render_photo(photo_id)
    except KeyError:
        abort(404)
    if request.args.get("quantized") == "1":
        settings = current_app.extensions["inktime_settings_repository"]
        profile_key = request.args.get("profile", str(settings.get("render.profile", "safe_4c")))
        dither = request.args.get("dither", str(settings.get("render.dither", "floyd_steinberg")))
        if profile_key not in DISPLAY_PROFILES or dither not in DITHER_ALGORITHMS:
            abort(400, description="RENDER-004 預覽 Profile 或抖動算法不合法")
        image = encode_image(
            image,
            profile_key=profile_key,
            dither=dither,
            color_distance=str(settings.get("render.color_distance", "oklab")),
            strength=float(settings.get("render.dither_strength", 1.0)),
        ).preview
    output = BytesIO()
    image.save(output, "PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png")


@bp.post("/api/v1/releases")
@administrator_required
def publish_release():
    payload = request.get_json(silent=True) or {}
    repository = current_app.extensions["inktime_job_repository"]
    profile_keys = [str(value) for value in payload.get("profile_keys", [])]
    if profile_keys and any(value not in DISPLAY_PROFILES for value in profile_keys):
        abort(400, description="RENDER-003 包含不支援的顯示 Profile")
    job_settings = {"photo_ids": [str(value) for value in payload.get("photo_ids", [])]}
    if profile_keys:
        job_settings["profile_keys"] = profile_keys
    job_id = repository.create_maintenance(
        kind="render",
        name="電子紙正式發布",
        settings=job_settings,
        created_by=g.user["id"],
    )
    current_app.extensions["inktime_job_service"].start(job_id)
    return {"id": job_id, "detail_url": f"/jobs/{job_id}"}, 202


@bp.get("/rendering/releases/<release_id>/<filename>")
@login_required
def release_preview(release_id: str, filename: str):
    if not filename.startswith("preview_") or not filename.endswith(".png"):
        abort(404)
    try:
        path = safe_join(current_app.config["INKTIME_RELEASE_DIR"], f"{release_id}/{filename}")
    except UnsafePathError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="image/png", conditional=True)


@bp.post("/api/v1/releases/<release_id>/rollback")
@administrator_required
def rollback_release(release_id: str):
    try:
        current_app.extensions["inktime_render_service"].rollback(release_id)
    except KeyError:
        abort(404)
    return {"status": "ok"}


@bp.get("/api/v1/fonts/preview")
@login_required
def preview_font():
    reference = str(request.args.get("reference", ""))
    manager = current_app.extensions["inktime_font_manager"]
    try:
        font_path = manager.validate_reference(reference, FONT_PREVIEW_TEXT)
        font = ImageFont.truetype(str(font_path), 38)
    except (OSError, ValueError) as exc:
        abort(422, description=f"IMG-002 {exc}")

    canvas = Image.new("RGB", (760, 116), "#f7f4eb")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((1, 1, 758, 114), radius=12, outline="#d8d1c2", width=2)
    draw.text((28, 28), FONT_PREVIEW_TEXT, font=font, fill="#1c241f")
    output = BytesIO()
    canvas.save(output, "PNG", optimize=True)
    output.seek(0)
    response = send_file(output, mimetype="image/png", max_age=3600)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


def _set_current_font(reference: str) -> None:
    current_app.extensions["inktime_settings_repository"].update(
        "render.font_path",
        reference,
        changed_by=g.user["id"],
        source_ip=request.remote_addr or "unknown",
    )


@bp.post("/api/v1/fonts/select")
@administrator_required
def select_font():
    payload = request.get_json(silent=True) or {}
    reference = str(payload.get("reference", "")).strip()
    manager = current_app.extensions["inktime_font_manager"]
    try:
        font_path = manager.validate_reference(reference, FONT_COMPATIBILITY_TEXT)
    except FontCoverageError as exc:
        abort(422, description=f"{exc.code} {exc}")
    except (OSError, ValueError) as exc:
        abort(400, description=f"IMG-002 {exc}")
    _set_current_font(reference)
    return {"reference": reference, "name": font_path.name, "status": "active"}


@bp.post("/api/v1/fonts")
@administrator_required
def upload_font():
    uploaded = request.files.get("font")
    if uploaded is None or not uploaded.filename:
        abort(400, description="IMG-002 請選擇字型檔案")
    filename = str(uploaded.filename).replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(filename).suffix.lower()
    if suffix not in {".ttf", ".otf", ".ttc"}:
        abort(400, description="IMG-002 只支援 TTF、OTF 或 TTC")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            size = 0
            while chunk := uploaded.stream.read(UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_FONT_BYTES:
                    abort(413, description="IMG-002 字型檔案不可超過 64 MiB")
                temporary.write(chunk)
        if size == 0:
            abort(400, description="IMG-002 字型檔案不可為空")
        manager = current_app.extensions["inktime_font_manager"]
        destination = manager.install(
            temporary_path,
            filename=filename,
            required_text=FONT_COMPATIBILITY_TEXT,
        )
    except FontCoverageError as exc:
        abort(422, description=f"{exc.code} {exc}")
    except (OSError, ValueError) as exc:
        abort(422, description=f"IMG-002 {exc}")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    reference = manager.reference_for_upload(destination)
    _set_current_font(reference)
    return {"name": destination.name, "reference": reference, "status": "active"}, 201
