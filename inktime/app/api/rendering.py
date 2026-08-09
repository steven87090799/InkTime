from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import secrets
import tempfile

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request, send_file
from PIL import Image, ImageDraw, ImageFont

from inktime.app.core.json_values import json_object_payload
from inktime.app.web.access import administrator_required, login_required
from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.domain.rendering import (
    BUILTIN_PHOTO_PRESETS,
    DISPLAY_PROFILES,
    DITHER_ALGORITHMS,
    FONT_COMPATIBILITY_TEXT,
    FONT_PREVIEW_TEXT,
    FontCoverageError,
    profile_summaries,
    resolve_layout_geometry,
)
from inktime.app.repositories.jobs import PreviewCapacityError
from inktime.app.services.rendering import (
    FIT_MODES,
    FRAME_ORIENTATIONS,
    LAYOUTS,
    PORTRAIT_ONLY_LAYOUTS,
)
from inktime.app.services.render_cache import RENDERER_VERSION
from inktime.app.domain.analysis.execution_mode import execution_mode


bp = Blueprint("rendering", __name__)
SIMULATOR_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
MAX_SIMULATOR_PHOTO_BYTES = 25 * 1024 * 1024
MAX_FONT_BYTES = 64 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
VIRTUAL_DISPLAY_POLL_SECONDS = 5
PREVIEW_USER_JOB_LIMIT = 2
PREVIEW_SYSTEM_JOB_LIMIT = 8


def _payload() -> dict:
    return json_object_payload(request, maximum_bytes=256 * 1024, error_prefix="RENDER-001")


@bp.get("/rendering")
@login_required
def rendering_page():
    settings = current_app.extensions["inktime_settings_repository"]
    render_service = current_app.extensions["inktime_render_service"]
    current_layout = str(settings.get("render.layout", "photo_info"))
    current_orientation = str(settings.get("render.frame_orientation", "portrait"))
    current_fit_mode = str(settings.get("render.fit_mode", "stretch_fill"))
    frame_geometries = {
        f"{layout}:{orientation}": resolve_layout_geometry(
            layout, orientation, 480, 800
        ).as_dict()
        for layout in LAYOUTS
        for orientation in ("portrait", "landscape")
    }
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
        show_location=bool(settings.get("render.show_location", True)),
        layouts=LAYOUTS,
        current_layout=current_layout,
        frame_orientations=FRAME_ORIENTATIONS,
        current_orientation=current_orientation,
        fit_modes=FIT_MODES,
        current_fit_mode=current_fit_mode,
        frame_geometries=frame_geometries,
        selection_mode=str(settings.get("render.selection_mode", "history_today")),
        candidate_photos=render_service.select_candidates_details(12),
        devices=[dict(device) for device in current_app.extensions["inktime_device_repository"].list()],
    )


@bp.get("/simulator")
@login_required
def simulator_page():
    settings = current_app.extensions["inktime_settings_repository"]
    custom_presets = _custom_photo_presets(settings)
    current_font_reference = str(settings.get("render.font_path", ""))
    fonts = current_app.extensions["inktime_font_manager"].options(current_font_reference)
    return render_template(
        "simulator.html",
        profiles=profile_summaries(),
        current_profile=str(settings.get("render.profile", "safe_4c")),
        current_dither=str(settings.get("render.dither", "floyd_steinberg")),
        dither_strength=float(settings.get("render.dither_strength", 1.0)),
        color_distance=str(settings.get("render.color_distance", "oklab")),
        photo_presets=BUILTIN_PHOTO_PRESETS,
        custom_photo_presets=custom_presets,
        fonts=[font for font in fonts if font.compatible],
        current_font=next((font for font in fonts if font.active and font.compatible), None),
        simulator_caption_max_chars=_simulator_caption_max_chars(settings),
        devices=[dict(device) for device in current_app.extensions["inktime_device_repository"].list()],
    )


def _custom_photo_presets(settings=None) -> dict:
    repository = settings or current_app.extensions["inktime_settings_repository"]
    try:
        value = json.loads(str(repository.get("render.custom_photo_presets", "{}")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_form(name: str, default):
    raw = request.form.get(name)
    if raw in {None, ""}:
        return default
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        abort(400, description=f"RENDER-007 {name} 必須是合法 JSON")
    return value


def _renderer_request() -> dict:
    requested_preset = str(request.form.get("preset", "photo_balanced"))
    custom = _custom_photo_presets().get(requested_preset)
    if custom:
        preset = str(custom.get("source_preset", "photo_balanced"))
        overrides = dict(custom.get("options", {}))
        stored_palette = custom.get("palette", {})
    else:
        preset = requested_preset
        overrides = {}
        stored_palette = {}
    incoming_options = _json_form("options", {})
    if not isinstance(incoming_options, dict):
        abort(400, description="RENDER-007 options 必須是 JSON 物件")
    overrides.update(incoming_options)
    palette = _json_form("palette", stored_palette)
    if not isinstance(palette, dict):
        abort(400, description="RENDER-006 palette 必須是 JSON 物件")
    mode = str(palette.get("mode", "default"))
    if mode not in {"default", "custom_rgb", "custom_lab"}:
        abort(400, description="RENDER-006 自訂色盤模式不合法")
    return {
        "requested_preset": requested_preset,
        "preset": preset,
        "overrides": overrides,
        "palette": palette,
        "palette_rgb": palette.get("rgb") if mode == "custom_rgb" else None,
        "palette_lab": palette.get("lab") if mode == "custom_lab" else None,
        "palette_version": str(palette.get("palette_version", "custom-1")),
        "text_regions": _json_form("text_regions", []),
        "face_regions": _json_form("face_regions", []),
    }


def _simulator_caption_max_chars(settings=None) -> int:
    repository = settings or current_app.extensions["inktime_settings_repository"]
    try:
        maximum = int(repository.get("analysis.side_caption_max_chars", 16))
    except (TypeError, ValueError):
        maximum = 16
    return max(1, min(maximum, 120))


def _simulator_preview_options() -> dict[str, str]:
    """Validate preview-only caption/font inputs without changing settings."""
    caption = str(request.form.get("caption", "")).strip()
    settings = current_app.extensions["inktime_settings_repository"]
    maximum = _simulator_caption_max_chars(settings)
    if len(caption) > maximum:
        abort(400, description=f"RENDER-007 模擬器短文案最多 {maximum} 字")
    font_reference = str(
        request.form.get("font_reference") or settings.get("render.font_path", "")
    ).strip()
    manager = current_app.extensions["inktime_font_manager"]
    try:
        manager.validate_reference(font_reference, f"{FONT_COMPATIBILITY_TEXT}{caption}")
    except (OSError, ValueError) as exc:
        abort(422, description=f"IMG-002 模擬器字型無法顯示短文案：{exc}")
    return {
        "caption": caption,
        "font_reference": font_reference,
        "font_root": str(manager.root),
        "font_builtin_root": str(manager.builtin_root),
    }


def _start_preview_job(name: str, settings: dict) -> tuple[dict, int]:
    repository = current_app.extensions["inktime_job_repository"]
    try:
        job_id = repository.create_maintenance_with_capacity(
            kind="render_preview",
            name=name,
            settings=settings,
            created_by=str(g.user["id"]),
            priority=6,
            per_user_limit=PREVIEW_USER_JOB_LIMIT,
            system_limit=PREVIEW_SYSTEM_JOB_LIMIT,
        )
    except PreviewCapacityError as exc:
        if exc.scope == "user":
            abort(429, description="RENDER-009 同一使用者最多可有 2 個 Preview Job")
        abort(503, description="RENDER-009 系統 Preview Job 已達固定上限")
    try:
        current_app.extensions["inktime_job_service"].start(job_id)
    except Exception:
        repository.cancel(job_id)
        raise
    return {
        "id": job_id,
        "job_id": job_id,
        "status_url": f"/api/v1/jobs/{job_id}",
        "detail_url": f"/jobs/{job_id}",
    }, 202


def _queue_upload_workload(operation: str, settings: dict) -> tuple[dict, int]:
    uploaded = request.files.get("photo")
    if uploaded is None or not uploaded.filename:
        abort(400, description="IMG-002 請選擇原始照片；Browser Canvas 不可直接發布")
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in SIMULATOR_IMAGE_SUFFIXES:
        abort(400, description="IMG-002 照片格式不支援")
    workload = current_app.extensions["inktime_render_workload_service"]
    workload.cleanup()
    try:
        token, photo_sha = workload.save_upload(
            uploaded.stream,
            suffix=suffix,
            max_bytes=MAX_SIMULATOR_PHOTO_BYTES,
        )
    except ValueError as exc:
        abort(413, description=str(exc))
    settings.update(
        {
            "operation": operation,
            "input_token": token,
            "input_suffix": suffix,
            "photo_sha": photo_sha,
            "timeout_seconds": 30,
        }
    )
    configuration = dict(settings.get("configuration", {}))
    settings["cache_fingerprint"] = {
        "photo_sha": photo_sha,
        "effective_orientation": 0,
        "orientation_source": "upload_exif_normalized",
        "manual_orientation_updated_at": None,
        "crop": None,
        "fit": settings.get("fit"),
        "layout": "renderer_compare",
        "secondary_photo_sha": None,
        "profile": settings.get("profile"),
        "panel_profile": settings.get("profile"),
        "palette": configuration.get("palette"),
        "configuration": configuration,
        "dither": dict(configuration.get("overrides", {})).get("dither"),
        "strength": dict(configuration.get("overrides", {})).get("error_strength", 1.0),
        "preset": configuration.get("requested_preset"),
        "caption": settings.get("caption", ""),
        "font_reference": settings.get("font_reference", ""),
        "renderer_version": RENDERER_VERSION,
        "font_version": None,
        "output_dimensions": [480, 800],
    }
    names = {
        "compare": "Renderer A/B Preview",
        "simulate": "電子紙模擬 Preview",
        "test_release": "Renderer 測試 Release",
    }
    try:
        return _start_preview_job(names[operation], settings)
    except Exception:
        workload.delete_input(token, suffix=suffix)
        raise


def _virtual_display_profile() -> str:
    settings = current_app.extensions["inktime_settings_repository"]
    profile_key = str(request.args.get("profile", settings.get("render.profile", "safe_4c")))
    if profile_key not in DISPLAY_PROFILES:
        abort(400, description="RENDER-003 不支援的虛擬墨水屏 Profile")
    return profile_key


def _latest_virtual_manifest(profile_key: str) -> dict:
    release_root = current_app.config["INKTIME_RELEASE_DIR"]
    latest_pointer = release_root / f"latest.{profile_key}"
    if not latest_pointer.exists() and profile_key == "safe_4c":
        latest_pointer = release_root / "latest"
    if not latest_pointer.is_file():
        abort(404, description="目前沒有可接收的電子紙發布版本")
    release_id = latest_pointer.read_text(encoding="utf-8").strip()
    try:
        manifest_path = safe_join(release_root, f"{release_id}/manifest.json")
    except UnsafePathError:
        abort(500, description="DEVICE-002 發布指標不合法")
    if not manifest_path.is_file():
        abort(404, description="找不到虛擬墨水屏發布 Manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        abort(500, description="DEVICE-002 發布 Manifest 無法讀取")
    if (
        manifest.get("release_id") != release_id
        or manifest.get("render_profile", "safe_4c") != profile_key
        or not isinstance(manifest.get("files"), list)
        or not manifest["files"]
    ):
        abort(500, description="DEVICE-002 發布 Manifest 與接收 Profile 不一致")
    return manifest


@bp.get("/virtual-display")
@login_required
def virtual_display_page():
    profile_key = _virtual_display_profile()
    profile = DISPLAY_PROFILES[profile_key]
    return render_template(
        "virtual_display.html",
        profile=profile,
        manifest_url=f"/api/v1/virtual-display/manifest?profile={profile_key}",
        poll_seconds=VIRTUAL_DISPLAY_POLL_SECONDS,
    )


@bp.get("/api/v1/virtual-display/manifest")
@login_required
def virtual_display_manifest():
    profile_key = _virtual_display_profile()
    manifest = _latest_virtual_manifest(profile_key)
    release_id = str(manifest["release_id"])
    manifest["download_base_url"] = f"/api/v1/virtual-display/releases/{release_id}/files/"
    manifest["receiver"] = {
        "mode": "read_only",
        "server_time": datetime.now(timezone.utc).isoformat(),
        "poll_seconds": VIRTUAL_DISPLAY_POLL_SECONDS,
    }
    response = jsonify(manifest)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-InkTime-Receiver"] = "virtual-display"
    return response


@bp.get("/api/v1/virtual-display/releases/<release_id>/files/<path:filename>")
@login_required
def virtual_display_file(release_id: str, filename: str):
    release_root = current_app.config["INKTIME_RELEASE_DIR"]
    try:
        manifest_path = safe_join(release_root, f"{release_id}/manifest.json")
        payload_path = safe_join(release_root, f"{release_id}/{filename}")
    except UnsafePathError:
        abort(400, description="PATH-001 路徑超出允許範圍")
    if not manifest_path.is_file() or not payload_path.is_file() or payload_path == manifest_path:
        abort(404)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        abort(500, description="DEVICE-002 發布 Manifest 無法讀取")
    file_entry = next(
        (
            item
            for item in manifest.get("files", [])
            if isinstance(item, dict) and item.get("name") == filename
        ),
        None,
    )
    if file_entry is None:
        abort(404, description="發布 Manifest 未列出此檔案")
    payload = payload_path.read_bytes()
    actual_sha256 = sha256(payload).hexdigest()
    if (
        len(payload) != int(file_entry.get("size", -1))
        or actual_sha256 != str(file_entry.get("sha256", "")).lower()
    ):
        abort(409, description="DEVICE-002 電子紙 Payload 大小或 SHA-256 驗證失敗")
    response = send_file(payload_path, mimetype="application/octet-stream", max_age=0)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-InkTime-Payload-SHA256"] = actual_sha256
    response.headers["X-InkTime-Payload-Bytes"] = str(len(payload))
    return response


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
    fit = str(request.form.get("fit", "stretch_fill"))
    try:
        strength = float(request.form.get("strength", "1"))
    except (TypeError, ValueError):
        abort(400, description="RENDER-004 抖動強度格式不合法")
    if profile_key not in DISPLAY_PROFILES or dither not in DITHER_ALGORITHMS:
        abort(400, description="RENDER-004 模擬 Profile 或抖動算法不合法")
    if fit not in FIT_MODES:
        abort(400, description="RENDER-004 圖片縮放模式不合法")

    return _queue_upload_workload(
        "simulate",
        {
            "profile": profile_key,
            "dither": dither,
            "color_distance": color_distance,
            "fit": fit,
            "strength": strength,
            **_simulator_preview_options(),
        },
    )


@bp.post("/api/v1/rendering/compare")
@login_required
def compare_renderer():
    profile_key = str(request.form.get("profile", "gdep073e01_6c"))
    fit = str(request.form.get("fit", "stretch_fill"))
    if profile_key not in DISPLAY_PROFILES:
        abort(400, description="RENDER-003 A/B 預覽 Profile 不合法")
    if fit not in FIT_MODES:
        abort(400, description="RENDER-004 圖片縮放模式不合法")
    configuration = _renderer_request()
    return _queue_upload_workload(
        "compare",
        {
            "profile": profile_key,
            "fit": fit,
            "configuration": configuration,
            **_simulator_preview_options(),
        },
    )


def _persist_custom_preset(payload: dict) -> dict:
    source_preset = str(payload.get("source_preset", "photo_balanced"))
    if source_preset not in BUILTIN_PHOTO_PRESETS:
        raise ValueError("RENDER-007 只能由內建照片 Preset 建立自訂副本")
    options = payload.get("options", {})
    palette = payload.get("palette", {})
    if not isinstance(options, dict) or not isinstance(palette, dict):
        raise ValueError("RENDER-007 Preset options 與 palette 必須是物件")
    # Resolve once to validate all supported fields without changing the built-in preset.
    from inktime.app.domain.rendering.photo_renderer import resolve_photo_options

    resolve_photo_options(source_preset, options)
    label = str(payload.get("label", "自訂照片 Preset")).strip()[:80]
    if not label:
        raise ValueError("RENDER-007 自訂 Preset 名稱不可空白")
    preset_id = str(payload.get("id", ""))
    existing = _custom_photo_presets()
    if preset_id not in existing:
        preset_id = f"custom-{secrets.token_hex(5)}"
    existing[preset_id] = {
        "id": preset_id,
        "label": label,
        "source_preset": source_preset,
        "options": options,
        "palette": palette,
    }
    encoded = json.dumps(existing, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 50_000:
        raise ValueError("RENDER-007 自訂 Preset 總資料量超過 50000 字元")
    current_app.extensions["inktime_settings_mutation_service"].update(
        "render.custom_photo_presets",
        encoded,
        changed_by=str(g.user["id"]),
        source_ip=request.remote_addr or "unknown",
    )
    return existing[preset_id]


@bp.post("/api/v1/rendering/presets")
@administrator_required
def save_photo_preset():
    payload = _payload()
    try:
        preset = _persist_custom_preset(payload)
    except (KeyError, TypeError, ValueError) as exc:
        abort(400, description=str(exc))
    return preset, 201


@bp.post("/api/v1/rendering/test-release")
@administrator_required
def publish_test_release():
    device_id = str(request.form.get("device_id", "")).strip()
    device = current_app.extensions["inktime_device_repository"].get(device_id)
    if device is None:
        abort(404, description="DEVICE-006 找不到測試裝置")
    device = dict(device)
    if not bool(device.get("enabled")):
        abort(409, description="DEVICE-006 已停用的裝置不可傳送測試 Release")
    profile_key = str(request.form.get("profile", "gdep073e01_6c"))
    if profile_key != str(device.get("panel_profile")):
        abort(409, description="DEVICE-006 測試色盤與裝置面板 Profile 不相容")
    configuration = _renderer_request()
    fit = str(request.form.get("fit", "stretch_fill"))
    if fit not in FIT_MODES:
        abort(400, description="RENDER-004 圖片縮放模式不合法")
    transport = str(request.form.get("transport", "custom")).strip()
    if transport not in {"custom", "stock_direct"}:
        abort(400, description="DEVICE-006 測試傳送邊界不合法")
    if transport == "stock_direct" and str(device.get("delivery_mode") or "") != "stock_compat":
        abort(409, description="DEVICE-008 Stock 測試只能選擇 Stock 相容裝置")
    if transport == "stock_direct":
        current_app.extensions[
            "inktime_device_release_service"
        ].cleanup_expired_stock_test_releases()
    delivery = str(request.form.get("delivery", "next_wake"))
    if delivery not in {"immediate", "next_wake"}:
        abort(400, description="DEVICE-006 測試傳送時機不合法")
    one_time = str(request.form.get("one_time", "true")).lower() in {"1", "true", "yes", "on"}
    restore_formal = str(request.form.get("restore_formal", "true")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return _queue_upload_workload(
        "test_release",
        {
            "device_id": device_id,
            "profile": profile_key,
            "fit": fit,
            "configuration": configuration,
            "delivery": delivery,
            "one_time": one_time,
            "restore_formal": restore_formal,
            "save_preset": transport == "custom"
            and str(request.form.get("save_preset", "false")).lower()
            in {"1", "true", "yes", "on"},
            "preset_label": str(request.form.get("preset_label", "測試後儲存")),
            "created_by": str(g.user["id"]),
            "transport": transport,
            **_simulator_preview_options(),
        },
    )


@bp.post("/api/v1/rendering/dual-pair-compare")
@administrator_required
def dual_pair_compare_preview():
    """Freeze one pair/caption/dither contract for the four formal previews."""
    payload = _payload()
    primary = str(payload.get("primary_photo_id", "")).strip()
    secondary = str(payload.get("secondary_photo_id", "")).strip()
    profile = str(payload.get("profile", "gdep073e01_6c"))
    fit_mode = str(payload.get("fit_mode", "stretch_fill"))
    if not primary or not secondary or primary == secondary:
        abort(400, description="RENDER-005 雙照片比較需要兩張不同照片")
    if profile not in DISPLAY_PROFILES or fit_mode not in FIT_MODES:
        abort(400, description="RENDER-005 Preview Profile 或縮放模式不合法")
    service = current_app.extensions["inktime_render_service"]
    try:
        seed = service.resolve_render_plan(
            primary,
            layout="photo_pair_caption",
            secondary_photo_id=secondary,
            orientation="portrait",
            fit_mode=fit_mode,
            profile=profile,
        )
        plans = [
            service.resolve_render_plan(
                primary,
                layout=layout,
                secondary_photo_id=secondary,
                orientation=orientation,
                fit_mode=fit_mode,
                profile=profile,
                primary_caption=seed["primary_caption"],
                secondary_caption=seed["secondary_caption"],
            )
            for layout, orientation in (
                ("photo_pair", "portrait"),
                ("photo_pair", "landscape"),
                ("photo_pair_caption", "portrait"),
                ("photo_pair_caption", "landscape"),
            )
        ]
    except (KeyError, ValueError) as exc:
        abort(409, description=str(exc))
    dither_values = {str(plan["effective_dither"]) for plan in plans}
    if len(dither_values) != 1:
        abort(409, description="RENDER-005 比較 Preview 必須使用同一個有效抖動決策")
    settings = current_app.extensions["inktime_settings_repository"]
    return _start_preview_job(
        "雙照片四種正式 Preview",
        {
            "operation": "dual_pair_compare",
            "plans": plans,
            "color_distance": str(settings.get("render.color_distance", "oklab")),
            "dither_strength": float(settings.get("render.dither_strength", 1.0)),
        },
    )


@bp.get("/api/v1/rendering/preview/<photo_id>")
@login_required
def preview(photo_id: str):
    layout = str(request.args.get("layout", "")).strip() or None
    if layout is not None and layout not in LAYOUTS:
        abort(400, description="RENDER-005 不支援的相框版型")
    crop_x = request.args.get("crop_x", type=float)
    crop_y = request.args.get("crop_y", type=float)
    secondary_photo_id = str(request.args.get("secondary_id", "")).strip() or None
    orientation = str(request.args.get("orientation", "")).strip() or None
    fit_mode = str(request.args.get("fit_mode", "")).strip() or None
    if orientation is not None and orientation not in FRAME_ORIENTATIONS:
        abort(400, description="RENDER-005 不支援的相框方向")
    if fit_mode is not None and fit_mode not in FIT_MODES:
        abort(400, description="RENDER-005 不支援的照片縮放方式")
    if (crop_x is None) != (crop_y is None) or any(
        value is not None and not 0 <= value <= 1 for value in (crop_x, crop_y)
    ):
        abort(400, description="RENDER-005 裁切位置必須同時提供且介於 0 到 1")
    settings = current_app.extensions["inktime_settings_repository"]
    render_service = current_app.extensions["inktime_render_service"]
    quantized = request.args.get("quantized") == "1"
    profile_key = request.args.get("profile", str(settings.get("render.profile", "safe_4c")))
    requested_dither = str(request.args.get("dither", "")).strip() or None
    if quantized and (
        profile_key not in DISPLAY_PROFILES
        or (requested_dither is not None and requested_dither not in DITHER_ALGORITHMS)
    ):
        abort(400, description="RENDER-004 預覽 Profile 或抖動算法不合法")
    arguments = {
        "photo_id": photo_id,
        "layout": layout,
        "crop_x": crop_x,
        "crop_y": crop_y,
        "secondary_photo_id": secondary_photo_id,
        "orientation": orientation,
        "fit_mode": fit_mode,
        "profile": profile_key if quantized else None,
        "requested_dither": requested_dither if quantized else None,
        "quantized": quantized,
    }
    try:
        fingerprint = render_service.preview_fingerprint(
            photo_id,
            layout=layout,
            crop_x=crop_x,
            crop_y=crop_y,
            secondary_photo_id=secondary_photo_id,
            orientation=orientation,
            fit_mode=fit_mode,
            profile=profile_key if quantized else None,
            dither=requested_dither if quantized else None,
        )
    except KeyError:
        abort(404)
    arguments["render_plan"] = fingerprint["render_plan"]
    arguments["layout"] = fingerprint["render_plan"]["layout"]
    arguments["secondary_photo_id"] = fingerprint["render_plan"]["secondary_photo_id"]
    arguments["primary_caption"] = fingerprint["render_plan"]["primary_caption"]
    arguments["secondary_caption"] = fingerprint["render_plan"]["secondary_caption"]
    arguments["orientation"] = fingerprint["render_plan"]["orientation"]
    arguments["fit_mode"] = fingerprint["render_plan"]["fit_mode"]
    arguments.update(
        {
            "profile": fingerprint["render_plan"]["profile"] if quantized else None,
            "dither": fingerprint["render_plan"]["effective_dither"] if quantized else None,
            "effective_dither": fingerprint["render_plan"]["effective_dither"] if quantized else None,
            "override_source": fingerprint["render_plan"]["override_source"] if quantized else None,
            "color_distance": fingerprint["render_settings"]["color_distance"],
            "dither_strength": fingerprint["render_settings"]["strength"],
        }
    )
    cache = current_app.extensions["inktime_render_cache"]
    cached = cache.get_bytes(fingerprint)
    if cached is None:
        return _start_preview_job(
            "Renderer 背景 Preview",
            {"operation": "library_preview", "arguments": arguments, "fingerprint": fingerprint},
        )
    layout_key = layout or str(settings.get("render.layout", "photo_info"))
    orientation_key = orientation or str(settings.get("render.frame_orientation", "portrait"))
    effective_orientation = "portrait" if layout_key in PORTRAIT_ONLY_LAYOUTS else orientation_key
    response = send_file(cached, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-InkTime-Layout"] = layout or str(settings.get("render.layout", "photo_info"))
    response.headers["X-InkTime-Orientation"] = effective_orientation
    response.headers["X-InkTime-Renderer-Cache"] = "hit"
    if quantized:
        response.headers["X-InkTime-Requested-Dither"] = str(
            fingerprint["render_plan"]["requested_dither"] or ""
        )
        response.headers["X-InkTime-Effective-Dither"] = str(fingerprint["render_plan"]["effective_dither"])
        response.headers["X-InkTime-Dither-Override-Source"] = str(
            fingerprint["render_plan"]["override_source"]
        )
    return response


@bp.get("/api/v1/rendering/background-results/<token>/<name>.png")
@login_required
def background_render_result(token: str, name: str):
    repository = current_app.extensions["inktime_job_repository"]
    if not repository.can_access_background_result(
        token,
        str(g.user["id"]),
        administrator=str(g.user["role"]) == "administrator",
    ):
        abort(404)
    try:
        path = current_app.extensions["inktime_render_workload_service"].result_path(token, name)
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    response = send_file(path, mimetype="image/png", conditional=True, max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.post("/api/v1/releases")
@administrator_required
def publish_release():
    payload = _payload()
    repository = current_app.extensions["inktime_job_repository"]
    profile_keys = [str(value) for value in payload.get("profile_keys", [])]
    if profile_keys and any(value not in DISPLAY_PROFILES for value in profile_keys):
        abort(400, description="RENDER-003 包含不支援的顯示 Profile")
    device_ids = [str(value).strip() for value in payload.get("device_ids", []) if str(value).strip()]
    if device_ids and profile_keys:
        abort(400, description="RENDER-003 裝置專屬發布不可同時指定 Profile")
    requested_photo_ids = [str(value) for value in payload.get("photo_ids", [])]
    if requested_photo_ids:
        try:
            current_app.extensions["inktime_render_candidate_repository"].require_for_execution_mode(
                requested_photo_ids,
                execution_mode(current_app.extensions["inktime_settings_repository"]),
            )
        except ValueError as exc:
            abort(409, description=str(exc))
    job_settings = {"photo_ids": requested_photo_ids}
    history = payload.get("history")
    if history is not None:
        if not isinstance(history, dict):
            abort(400, description="HISTORY-001 history 必須是物件")
        history_date = str(history.get("history_date", ""))
        try:
            datetime.strptime(history_date, "%Y-%m-%d")
        except ValueError:
            abort(400, description="HISTORY-001 history_date 必須是 YYYY-MM-DD")
        job_settings["history"] = {
            "history_date": history_date,
            "selection_method": str(history.get("selection_method", "manual"))[:80],
        }
    if profile_keys:
        job_settings["profile_keys"] = profile_keys
    if device_ids:
        job_settings["device_ids"] = device_ids
    job_id = repository.create_maintenance(
        kind="render",
        name="電子紙正式發布",
        settings=job_settings,
        created_by=g.user["id"],
    )
    current_app.extensions["inktime_job_service"].start(job_id)
    return {"id": job_id, "detail_url": f"/jobs/{job_id}"}, 202


def _history_response(selection: dict) -> dict:
    if selection.get("status") != "ok":
        return selection
    settings = current_app.extensions["inktime_settings_repository"]
    for candidate in selection["candidates"]:
        candidate["renderer"] = "server"
        candidate["render_profile"] = str(settings.get("render.profile", "safe_4c"))
        candidate["palette_version"] = str(settings.get("render.profile", "safe_4c"))
        candidate["preset"] = str(settings.get("render.layout", "photo_info"))
        candidate["dither"] = str(settings.get("render.dither", "floyd_steinberg"))
        candidate["render_ms"] = None
    return selection


@bp.post("/api/v1/rendering/history/select")
@administrator_required
def select_history_day():
    try:
        selection = current_app.extensions["inktime_render_service"].select_random_history_day(_payload())
    except ValueError as exc:
        abort(400, description=str(exc))
    return _history_response(selection)


@bp.post("/api/v1/rendering/history/reroll")
@administrator_required
def reroll_history_day():
    try:
        selection = current_app.extensions["inktime_render_service"].reroll_history_day(_payload())
    except ValueError as exc:
        abort(400, description=str(exc))
    return _history_response(selection)


@bp.post("/api/v1/rendering/history/test-release")
@administrator_required
def publish_history_test_release():
    payload = _payload()
    photo_id = str(payload.get("photo_id", "")).strip()
    device_id = str(payload.get("device_id", "")).strip()
    device = current_app.extensions["inktime_device_repository"].get(device_id)
    if not photo_id:
        abort(400, description="HISTORY-001 必須選擇照片")
    if device is None or not bool(device["enabled"]):
        abort(404, description="DEVICE-006 找不到可用測試裝置")
    settings = current_app.extensions["inktime_settings_repository"]
    profile_key = str(settings.get("render.profile", "safe_4c"))
    if profile_key != str(device["panel_profile"]):
        abort(409, description="DEVICE-006 目前渲染 Profile 與裝置面板不相容")
    photo = current_app.extensions["inktime_photo_repository"].get_with_path(photo_id)
    if photo is None:
        abort(404, description="HISTORY-001 找不到照片")
    try:
        source = safe_join(Path(photo["root_path"]), str(photo["relative_path"]))
    except UnsafePathError:
        abort(404, description="HISTORY-001 照片路徑不合法")
    workload = current_app.extensions["inktime_render_workload_service"]
    try:
        token, photo_sha, suffix = workload.save_file(source, max_bytes=MAX_SIMULATOR_PHOTO_BYTES)
    except ValueError as exc:
        abort(413, description=str(exc))
    job_settings = {
        "operation": "history_test_release",
        "input_token": token,
        "input_suffix": suffix,
        "photo_sha": photo_sha,
        "source_photo_id": photo_id,
        "device_id": device_id,
        "profile": profile_key,
        "fit": str(settings.get("render.fit_mode", "stretch_fill")),
        "delivery": str(payload.get("delivery", "next_wake")),
        "one_time": True,
        "restore_formal": True,
        "save_preset": False,
        "created_by": str(g.user["id"]),
        "timeout_seconds": 30,
        "configuration": {
            "requested_preset": "photo_balanced",
            "preset": "photo_balanced",
            "overrides": {
                "dither": str(settings.get("render.dither", "floyd_steinberg")),
                "color_distance": str(settings.get("render.color_distance", "oklab")),
                "error_strength": float(settings.get("render.dither_strength", 1.0)),
            },
            "palette": {"mode": "default"},
            "palette_rgb": None,
            "palette_lab": None,
            "palette_version": "builtin",
            "text_regions": [],
            "face_regions": [],
        },
    }
    try:
        return _start_preview_job("歷史照片測試 Release", job_settings)
    except Exception:
        workload.delete_input(token, suffix=suffix)
        raise


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
    current_app.extensions["inktime_settings_mutation_service"].update(
        "render.font_path",
        reference,
        changed_by=g.user["id"],
        source_ip=request.remote_addr or "unknown",
    )


@bp.post("/api/v1/fonts/select")
@administrator_required
def select_font():
    payload = _payload()
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
