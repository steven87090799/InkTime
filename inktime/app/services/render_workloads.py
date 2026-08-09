from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import BinaryIO, Any
from uuid import uuid4

from PIL import Image, ImageDraw
from PIL import ImageOps

from inktime.app.domain.rendering import (
    AtomicReleasePublisher,
    DeviceTestReleaseStore,
    FONT_COMPATIBILITY_TEXT,
    FontManager,
    encode_image,
    palette_for_profile,
    render_photo,
)
from inktime.app.db import Database
from inktime.app.domain.photos import LocationResolver
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.render_candidates import RenderCandidateRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.services.device_releases import payload_entry_from_manifest
from inktime.app.services.release_coordinator import ReleaseCoordinator
from inktime.app.services.rendering import (
    PORTRAIT_ONLY_LAYOUTS,
    RenderService,
    draw_caption_footer,
)
from inktime.app.services.weather import WeatherService
from inktime.app.workers.process_boundary import ProcessCallError


MAX_INPUT_PIXELS = 40_000_000


def _atomic_png_file(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        image.save(temporary, "PNG", optimize=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _open_saved_upload(path_value: str, suffix: str) -> tuple[Image.Image, str]:
    path = Path(path_value)
    if suffix in {".heic", ".heif"}:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    with Image.open(path) as opened:
        if opened.width * opened.height > MAX_INPUT_PIXELS:
            raise ValueError("IMG-002 照片像素不可超過 4000 萬")
        opened.load()
        source_size = f"{opened.width}x{opened.height}"
        image = ImageOps.exif_transpose(opened).convert("RGB")
    return image, source_size


def _photo_renderer_fit(image: Image.Image, fit: str) -> tuple[Image.Image, str]:
    """Adapt the shared fit contract to the legacy A/B renderer boundary.

    ``photo_renderer.prepare_photo_canvas`` intentionally accepts only its
    historical contain/cover modes.  Normalizing stretch_fill here keeps the
    boundary single and gives that renderer an exact-aspect input, so its
    existing cover operation cannot crop or letterbox any pixels.
    """
    if fit == "stretch_fill":
        return image.resize((480, 800), Image.Resampling.LANCZOS), "cover"
    return image, fit


def _overlay_simulator_caption(
    image: Image.Image,
    *,
    caption: str,
    font_manager: FontManager,
    font_reference: str,
) -> Image.Image:
    """Add the simulator-only caption through the formal footer helper."""
    text = str(caption or "").strip()
    if not text:
        return image
    font_path = font_manager.validate_reference(
        font_reference, f"{FONT_COMPATIBILITY_TEXT}{text}"
    )
    preview = image.copy().convert("RGB")
    draw = ImageDraw.Draw(preview)
    footer_top = max(0, preview.height - 120)
    draw.rectangle((0, footer_top, preview.width, preview.height), fill="white")
    draw.line((20, footer_top + 4, preview.width - 20, footer_top + 4), fill="black", width=2)
    draw_caption_footer(
        draw,
        text,
        font_path=font_path,
        x=22,
        top=footer_top + 12,
        bottom=preview.height - 12,
        width=max(1, preview.width - 44),
        fill="black",
        wrap_enabled=True,
        maximum_lines=2,
        minimum_font_size=17,
    )
    return preview


def _captioned_renderer_outputs(
    result,
    *,
    settings: dict[str, Any],
    profile_key: str,
    profile,
):
    """Freeze one legal ephemeral caption into both preview and payload paths."""
    caption = str(settings.get("caption", "")).strip()
    if not caption:
        return result.source, result.encoded
    font_manager = FontManager(
        Path(str(settings["font_root"])), Path(str(settings["font_builtin_root"]))
    )
    source = _overlay_simulator_caption(
        result.source,
        caption=caption,
        font_manager=font_manager,
        font_reference=str(settings.get("font_reference", "")),
    )
    processed = _overlay_simulator_caption(
        result.processed,
        caption=caption,
        font_manager=font_manager,
        font_reference=str(settings.get("font_reference", "")),
    )
    encoded = encode_image(
        processed,
        profile_key=profile_key,
        profile=profile,
        dither=str(result.options["dither"]),
        color_distance=str(result.options["color_distance"]),
        strength=float(result.options["error_strength"]),
        linear_light=bool(result.options.get("linear_light", True)),
        protected_mask=result.protected_mask,
    )
    return source, encoded


def _prepare_compare_child(
    *, settings: dict[str, Any], input_path: str, prepared_path: str
) -> dict[str, Any]:
    prepared = Path(prepared_path)
    prepared.mkdir(mode=0o700, parents=True)
    image, source_size = _open_saved_upload(input_path, str(settings["input_suffix"]))
    configuration = dict(settings["configuration"])
    renderer_image, renderer_fit = _photo_renderer_fit(image, str(settings["fit"]))
    result = render_photo(
        renderer_image,
        profile_key=str(settings["profile"]),
        preset=str(configuration["preset"]),
        overrides=dict(configuration["overrides"]),
        fit=renderer_fit,
        palette_rgb=configuration.get("palette_rgb"),
        palette_lab=configuration.get("palette_lab"),
        palette_version=str(configuration["palette_version"]),
        text_regions=list(configuration.get("text_regions", [])),
        face_regions=list(configuration.get("face_regions", [])),
    )
    profile = palette_for_profile(
        str(settings["profile"]),
        rgb_values=configuration.get("palette_rgb"),
        lab_values=configuration.get("palette_lab"),
        palette_version=str(configuration["palette_version"]),
    )
    captioned_source, captioned_encoded = _captioned_renderer_outputs(
        result,
        settings=settings,
        profile_key=str(settings["profile"]),
        profile=profile,
    )
    started = datetime.now(timezone.utc)
    legacy = encode_image(
        captioned_source,
        profile_key=str(settings["profile"]),
        dither="gooddisplay",
        color_distance="rgb",
        strength=1.0,
    )
    legacy_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    outputs = {
        "original": captioned_source,
        "legacy": legacy.preview,
        "new": captioned_encoded.preview,
    }
    for name, output in {
        "original": outputs["original"],
        "legacy": outputs["legacy"],
        "new": outputs["new"],
    }.items():
        _atomic_png_file(prepared / f"{name}.png", output)
    return {
        "source_size": source_size,
        "payload_bytes": len(captioned_encoded.payload),
        "render_ms": result.render_ms,
        "legacy_render_ms": legacy_ms,
        "preset": str(configuration["requested_preset"]),
        "source_preset": result.preset,
        "dither": result.options["dither"],
        "color_distance": result.options["color_distance"],
        "linear_light": bool(result.options.get("linear_light")),
        "palette_version": profile.palette_version,
        "palette": RenderWorkloadService._palette_statistics(
            captioned_encoded.preview, captioned_encoded.palette
        ),
        "publish_source": "server_original_upload_only",
        "model": "disabled",
        "cache_hit": False,
        "stage": "preview_completed",
    }


def _prepare_simulator_child(
    *, settings: dict[str, Any], input_path: str, prepared_path: str
) -> dict[str, Any]:
    prepared = Path(prepared_path)
    prepared.mkdir(mode=0o700, parents=True)
    image, source_size = _open_saved_upload(input_path, str(settings["input_suffix"]))
    size = (480, 800)
    fit = str(settings["fit"])
    if fit == "stretch_fill":
        canvas = image.resize(size, Image.Resampling.LANCZOS)
    elif fit == "cover":
        canvas = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
    else:
        fitted = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size, "white")
        canvas.paste(
            fitted,
            ((canvas.width - fitted.width) // 2, (canvas.height - fitted.height) // 2),
        )
    caption = str(settings.get("caption", "")).strip()
    if caption:
        caption_fonts = FontManager(
            Path(str(settings["font_root"])), Path(str(settings["font_builtin_root"]))
        )
        canvas = _overlay_simulator_caption(
            canvas,
            caption=caption,
            font_manager=caption_fonts,
            font_reference=str(settings.get("font_reference", "")),
        )
    encoded = encode_image(
        canvas,
        profile_key=str(settings["profile"]),
        dither=str(settings["dither"]),
        color_distance=str(settings["color_distance"]),
        strength=float(settings["strength"]),
    )
    _atomic_png_file(prepared / "preview.png", encoded.preview)
    return {
        "source_size": source_size,
        "payload_bytes": len(encoded.payload),
        "profile": str(settings["profile"]),
        "dither": str(settings["dither"]),
        "stage": "preview_completed",
    }


def _prepare_test_release_child(
    *, settings: dict[str, Any], input_path: str, prepared_path: str
) -> dict[str, Any]:
    prepared = Path(prepared_path)
    prepared.mkdir(mode=0o700, parents=True)
    image, source_size = _open_saved_upload(input_path, str(settings["input_suffix"]))
    configuration = dict(settings["configuration"])
    renderer_image, renderer_fit = _photo_renderer_fit(image, str(settings["fit"]))
    result = render_photo(
        renderer_image,
        profile_key=str(settings["profile"]),
        preset=str(configuration["preset"]),
        overrides=dict(configuration["overrides"]),
        fit=renderer_fit,
        palette_rgb=configuration.get("palette_rgb"),
        palette_lab=configuration.get("palette_lab"),
        palette_version=str(configuration["palette_version"]),
        text_regions=list(configuration.get("text_regions", [])),
        face_regions=list(configuration.get("face_regions", [])),
    )
    profile = palette_for_profile(
        str(settings["profile"]),
        rgb_values=configuration.get("palette_rgb"),
        lab_values=configuration.get("palette_lab"),
        palette_version=str(configuration["palette_version"]),
    )
    _source, encoded = _captioned_renderer_outputs(
        result,
        settings=settings,
        profile_key=str(settings["profile"]),
        profile=profile,
    )
    (prepared / "payload.bin").write_bytes(encoded.payload)
    _atomic_png_file(prepared / "preview.png", encoded.preview)
    return {
        "source_size": source_size,
        "preset": configuration["requested_preset"],
        "source_preset": result.preset,
        "pipeline": result.options,
        "dither": str(result.options["dither"]),
        "color_distance": str(result.options["color_distance"]),
        "dither_strength": float(result.options["error_strength"]),
        "linear_light": bool(result.options.get("linear_light")),
        "palette_version": profile.palette_version,
        "palette": [
            {"code": color.code, "name": color.name, "rgb": list(color.rgb)}
            for color in encoded.palette
        ],
        "caption": str(settings.get("caption", "")).strip(),
        "font_reference": str(settings.get("font_reference", "")),
    }


def _prepare_library_preview_child(
    *,
    settings: dict[str, Any],
    database_path: str,
    prepared_path: str,
    font_root: str,
    font_builtin_root: str,
    location_csv: str,
) -> dict[str, Any]:
    """Render only against a private database snapshot and prepared directory."""

    prepared = Path(prepared_path)
    prepared.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = Database(Path(database_path))
    settings_repository = SettingsRepository(database)
    publisher = AtomicReleasePublisher(prepared / "release-sandbox")
    service = RenderService(
        database,
        PhotoRepository(database),
        settings_repository,
        FontManager(Path(font_root), Path(font_builtin_root)),
        publisher,
        RenderCandidateRepository(database),
        ReleaseCoordinator(database, publisher),
        LocationResolver(Path(location_csv)),
        WeatherService(settings_repository),
    )
    arguments = dict(settings["arguments"])
    image = service.render_photo(
        str(arguments["photo_id"]),
        layout=arguments.get("layout"),
        crop_x=arguments.get("crop_x"),
        crop_y=arguments.get("crop_y"),
        secondary_photo_id=arguments.get("secondary_photo_id"),
        orientation=arguments.get("orientation"),
        fit_mode=arguments.get("fit_mode"),
        primary_caption=arguments.get("primary_caption"),
        secondary_caption=arguments.get("secondary_caption"),
    )
    quantized = bool(arguments.get("quantized"))
    if quantized:
        image = encode_image(
            image,
            profile_key=str(arguments["profile"]),
            dither=str(arguments["effective_dither"]),
            color_distance=str(arguments["color_distance"]),
            strength=float(arguments["dither_strength"]),
        ).preview
    layout_key = str(arguments.get("layout") or settings_repository.get("render.layout", "photo_info"))
    orientation_key = str(
        arguments.get("orientation") or settings_repository.get("render.frame_orientation", "portrait")
    )
    if ("portrait" if layout_key in PORTRAIT_ONLY_LAYOUTS else orientation_key) == "landscape":
        image = image.transpose(Image.Transpose.ROTATE_90)
    _atomic_png_file(prepared / "preview.png", image)
    return {
        "stage": "preview_completed",
        "width": image.width,
        "height": image.height,
        "quantized": quantized,
        "requested_dither": arguments.get("requested_dither"),
        "effective_dither": arguments.get("effective_dither"),
        "override_source": arguments.get("override_source"),
    }


def _prepare_dual_pair_compare_child(
    *,
    settings: dict[str, Any],
    database_path: str,
    prepared_path: str,
    font_root: str,
    font_builtin_root: str,
    location_csv: str,
) -> dict[str, Any]:
    """Render the four frozen dual-photo plans in one isolated renderer child."""
    prepared = Path(prepared_path)
    prepared.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = Database(Path(database_path))
    settings_repository = SettingsRepository(database)
    publisher = AtomicReleasePublisher(prepared / "release-sandbox")
    service = RenderService(
        database,
        PhotoRepository(database),
        settings_repository,
        FontManager(Path(font_root), Path(font_builtin_root)),
        publisher,
        RenderCandidateRepository(database),
        ReleaseCoordinator(database, publisher),
        LocationResolver(Path(location_csv)),
        WeatherService(settings_repository),
    )
    previews: list[dict[str, Any]] = []
    for index, plan in enumerate(settings["plans"]):
        image = service._render_plan_image(dict(plan))
        image = encode_image(
            image,
            profile_key=str(plan["profile"]),
            dither=str(plan["effective_dither"]),
            color_distance=str(settings["color_distance"]),
            strength=float(settings["dither_strength"]),
        ).preview
        if str(plan["orientation"]) == "landscape":
            image = image.transpose(Image.Transpose.ROTATE_90)
        name = f"preview_{index}.png"
        _atomic_png_file(prepared / name, image)
        previews.append(
            {
                "name": name,
                "layout": plan["layout"],
                "orientation": plan["orientation"],
                "profile": plan["profile"],
                "effective_dither": plan["effective_dither"],
                "primary_photo_id": plan["primary_photo_id"],
                "secondary_photo_id": plan["secondary_photo_id"],
                "primary_caption": plan["primary_caption"],
                "secondary_caption": plan.get("secondary_caption"),
            }
        )
    return {"stage": "dual_pair_compare_completed", "previews": previews}


class RenderWorkloadService:
    """Persist bounded private inputs/results for existing background Jobs."""

    def __init__(
        self,
        root: Path,
        publisher,
        devices,
        release_dir: Path,
        settings_repository,
        process_boundary,
        job_repository,
    ) -> None:
        self.root = root.resolve()
        self.input_root = self.root / "inputs"
        self.result_root = self.root / "results"
        self.prepared_root = self.root / "prepared"
        self.input_root.mkdir(parents=True, exist_ok=True)
        self.result_root.mkdir(parents=True, exist_ok=True)
        self.prepared_root.mkdir(parents=True, exist_ok=True)
        self.publisher = publisher
        self.devices = devices
        self.release_dir = release_dir.resolve()
        self.settings = settings_repository
        self.process_boundary = process_boundary
        self.jobs = job_repository
        self.retention = timedelta(days=2)
        self.max_result_entries = 128
        self.max_result_bytes = 256 * 1024 * 1024
        self._metrics = {"compare_cache_hit": 0, "compare_cache_miss": 0}

    @staticmethod
    def _validate_token(token: str) -> str:
        if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
            raise ValueError("RENDER-008 背景渲染識別碼不合法")
        return token

    @staticmethod
    def _atomic_png(path: Path, image: Image.Image) -> None:
        _atomic_png_file(path, image)

    def save_input(self, image: Image.Image) -> tuple[str, str]:
        token = uuid4().hex
        destination = self.input_root / f"{token}.png"
        self._atomic_png(destination, image.convert("RGB"))
        return token, sha256(destination.read_bytes()).hexdigest()

    def save_upload(self, stream: BinaryIO, *, suffix: str, max_bytes: int) -> tuple[str, str]:
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}:
            raise ValueError("IMG-002 照片格式不支援")
        token = uuid4().hex
        destination = self.input_root / f"{token}{suffix}"
        handle, temporary_name = tempfile.mkstemp(prefix=f".{token}-", suffix=".upload", dir=self.input_root)
        os.close(handle)
        temporary = Path(temporary_name)
        digest = sha256()
        size = 0
        try:
            with temporary.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > int(max_bytes):
                        raise ValueError("IMG-002 照片不可超過 25 MiB")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise ValueError("IMG-002 照片內容不可為空")
            os.replace(temporary, destination)
            return token, digest.hexdigest()
        finally:
            temporary.unlink(missing_ok=True)

    def save_file(self, source: Path, *, max_bytes: int) -> tuple[str, str, str]:
        suffix = source.suffix.lower()
        if not source.is_file() or source.stat().st_size > int(max_bytes):
            raise ValueError("IMG-002 照片不可超過 25 MiB")
        with source.open("rb") as stream:
            token, digest = self.save_upload(stream, suffix=suffix, max_bytes=max_bytes)
        return token, digest, suffix

    def input_path(self, token: str, *, suffix: str) -> Path:
        self._validate_token(token)
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}:
            raise ValueError("invalid input suffix")
        return self.input_root / f"{token}{suffix}"

    def delete_input(self, token: str, *, suffix: str) -> None:
        self.input_path(token, suffix=suffix).unlink(missing_ok=True)

    def _open_input(self, token: str) -> Image.Image:
        path = self.input_root / f"{self._validate_token(token)}.png"
        with Image.open(path) as opened:
            opened.load()
            return opened.convert("RGB")

    def _result_url(self, token: str, name: str) -> str:
        return f"/api/v1/rendering/background-results/{token}/{name}.png"

    def result_path(self, token: str, name: str) -> Path:
        self._validate_token(token)
        if name not in {"original", "legacy", "new", "preview"}:
            raise ValueError("invalid render result name")
        return self.result_root / token / f"{name}.png"

    def save_background_preview(self, image: Image.Image) -> str:
        token = uuid4().hex
        self._atomic_png(self.result_path(token, "preview"), image)
        self.cleanup()
        return self._result_url(token, "preview")

    @staticmethod
    def _private_database_snapshot(source: Path, destination: Path) -> None:
        """Copy a consistent SQLite view and remove credentials from the child copy."""

        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        source_connection = sqlite3.connect(source)
        snapshot_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(snapshot_connection)
            snapshot_connection.execute("DELETE FROM secrets")
            snapshot_connection.execute("UPDATE users SET password_hash=''")
            snapshot_connection.execute("UPDATE devices SET token_hash='preview-' || id")
            snapshot_connection.commit()
        finally:
            snapshot_connection.close()
            source_connection.close()

    def library_preview(
        self,
        settings: dict[str, Any],
        commit_context: dict[str, str],
        *,
        render_service,
        render_cache,
    ) -> dict[str, Any]:
        """Prepare in a spawn child; commit cache/result only for the lease owner."""

        context = dict(commit_context)
        fingerprint = dict(settings["fingerprint"])

        def cancelled() -> bool:
            return not self.jobs.can_commit_item(
                context["job_id"],
                context["item_id"],
                context["worker_id"],
                context["idempotency_key"],
            )

        if cancelled():
            raise ProcessCallError("library preview lease lost")
        image = render_cache.get(fingerprint)
        if image is not None:
            if cancelled():
                raise ProcessCallError("library preview lease lost")
            preview_url = self.save_background_preview(image)
            result_token = preview_url.split("/")[-2]
            cached_result_path = self.result_root / result_token
            if cancelled():
                shutil.rmtree(cached_result_path, ignore_errors=True)
                raise ProcessCallError("library preview lease lost")
            return {
                "stage": "preview_completed",
                "preview_url": preview_url,
                "cache_hit": True,
            }

        prepared = self.prepared_root / uuid4().hex
        cache_path: Path | None = None
        result_path: Path | None = None
        try:
            prepared.mkdir(mode=0o700, parents=True)
            snapshot_path = prepared / "render.db"
            self._private_database_snapshot(Path(render_service.database.path), snapshot_path)
            if cancelled():
                raise ProcessCallError("library preview lease lost")
            child_result = self.process_boundary.call(
                _prepare_library_preview_child,
                timeout_seconds=float(settings.get("timeout_seconds", 30)),
                kwargs={
                    "settings": {
                        "arguments": dict(settings["arguments"]),
                    },
                    "database_path": str(snapshot_path),
                    "prepared_path": str(prepared),
                    "font_root": str(render_service.fonts.root),
                    "font_builtin_root": str(render_service.fonts.builtin_root),
                    "location_csv": str(render_service.locations.csv_path),
                },
                cancel_requested=cancelled,
                process_name="inktime-library-preview-child",
            )
            if cancelled():
                raise ProcessCallError("library preview lease lost")
            with Image.open(prepared / "preview.png") as opened:
                opened.load()
                image = opened.convert("RGB")
            if cancelled():
                raise ProcessCallError("library preview lease lost")
            cache_key = render_cache.put(fingerprint, image)
            cache_path = render_cache.root / f"{cache_key}.png"
            if cancelled():
                cache_path.unlink(missing_ok=True)
                raise ProcessCallError("library preview lease lost")
            preview_url = self.save_background_preview(image)
            result_token = preview_url.split("/")[-2]
            result_path = self.result_root / result_token
            if cancelled():
                cache_path.unlink(missing_ok=True)
                shutil.rmtree(result_path, ignore_errors=True)
                raise ProcessCallError("library preview lease lost")
            return {
                **dict(child_result),
                "preview_url": preview_url,
                "cache_hit": False,
            }
        except Exception:
            if cancelled():
                if cache_path is not None:
                    cache_path.unlink(missing_ok=True)
                if result_path is not None:
                    shutil.rmtree(result_path, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(prepared, ignore_errors=True)

    def dual_pair_compare(
        self, settings: dict[str, Any], commit_context: dict[str, str], *, render_service
    ) -> dict[str, Any]:
        """One frozen background job produces all four formal renderer previews."""
        context = dict(commit_context)
        if not self.jobs.can_commit_item(
            context["job_id"], context["item_id"], context["worker_id"], context["idempotency_key"]
        ):
            raise ProcessCallError("dual preview lease lost")
        prepared = self.prepared_root / uuid4().hex
        try:
            prepared.mkdir(mode=0o700, parents=True)
            snapshot_path = prepared / "render.db"
            self._private_database_snapshot(Path(render_service.database.path), snapshot_path)
            result = self.process_boundary.call(
                _prepare_dual_pair_compare_child,
                timeout_seconds=float(settings.get("timeout_seconds", 45)),
                kwargs={
                    "settings": {
                        "plans": list(settings["plans"]),
                        "color_distance": settings["color_distance"],
                        "dither_strength": settings["dither_strength"],
                    },
                    "database_path": str(snapshot_path),
                    "prepared_path": str(prepared),
                    "font_root": str(render_service.fonts.root),
                    "font_builtin_root": str(render_service.fonts.builtin_root),
                    "location_csv": str(render_service.locations.csv_path),
                },
                process_name="inktime-dual-pair-preview-child",
            )
            previews = []
            for item in result["previews"]:
                with Image.open(prepared / item["name"]) as opened:
                    preview_url = self.save_background_preview(opened.convert("RGB"))
                previews.append({**item, "preview_url": preview_url})
            return {"stage": result["stage"], "previews": previews}
        finally:
            shutil.rmtree(prepared, ignore_errors=True)

    @staticmethod
    def _atomic_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _cached_compare(self, token: str) -> dict | None:
        metadata = self.result_root / token / "result.json"
        try:
            result = json.loads(metadata.read_text(encoding="utf-8"))
            for name in ("original", "legacy", "new"):
                with Image.open(self.result_path(token, name)) as opened:
                    opened.verify()
            if not isinstance(result, dict):
                raise ValueError("invalid renderer metadata")
        except (OSError, ValueError, json.JSONDecodeError):
            for path in (self.result_root / token).glob("*"):
                path.unlink(missing_ok=True)
            if (self.result_root / token).exists():
                (self.result_root / token).rmdir()
            return None
        for name in ("original", "legacy", "new"):
            result[name] = self._result_url(token, name)
        os.utime(metadata, None)
        return result

    @staticmethod
    def _palette_statistics(image: Image.Image, colors) -> list[dict]:
        counts: Counter[Any] = Counter(image.convert("RGB").getdata())
        total = image.width * image.height
        return [
            {
                "name": color.name,
                "rgb": list(color.rgb),
                "pixels": counts[color.rgb],
                "ratio": round(counts[color.rgb] / total, 6),
            }
            for color in colors
        ]

    def compare(self, settings: dict) -> dict:
        input_token = self._validate_token(str(settings["input_token"]))
        suffix = str(settings["input_suffix"])
        fingerprint = json.dumps(
            dict(settings["cache_fingerprint"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        token = sha256(fingerprint.encode("utf-8")).hexdigest()[:32]
        cached = self._cached_compare(token)
        if cached is not None:
            self.delete_input(input_token, suffix=suffix)
            cached["cache_hit"] = True
            return cached
        prepared = self.prepared_root / uuid4().hex
        try:
            response = self.process_boundary.call(
                _prepare_compare_child,
                timeout_seconds=float(settings.get("timeout_seconds", 30)),
                kwargs={
                    "settings": settings,
                    "input_path": str(self.input_path(input_token, suffix=suffix)),
                    "prepared_path": str(prepared),
                },
                process_name="inktime-render-child",
            )
            final = self.result_root / token
            if final.exists():
                shutil.rmtree(final)
            prepared.replace(final)
        except Exception:
            shutil.rmtree(prepared, ignore_errors=True)
            raise
        response.update(
            {
                "original": self._result_url(token, "original"),
                "legacy": self._result_url(token, "legacy"),
                "new": self._result_url(token, "new"),
            }
        )
        self._atomic_json(self.result_root / token / "result.json", response)
        self.delete_input(input_token, suffix=suffix)
        self.cleanup()
        return response

    def simulate(self, settings: dict) -> dict:
        token = self._validate_token(str(settings["input_token"]))
        suffix = str(settings["input_suffix"])
        prepared = self.prepared_root / uuid4().hex
        try:
            result = self.process_boundary.call(
                _prepare_simulator_child,
                timeout_seconds=float(settings.get("timeout_seconds", 30)),
                kwargs={
                    "settings": settings,
                    "input_path": str(self.input_path(token, suffix=suffix)),
                    "prepared_path": str(prepared),
                },
                process_name="inktime-render-child",
            )
            final = self.result_root / token
            if final.exists():
                shutil.rmtree(final)
            prepared.replace(final)
        except Exception:
            shutil.rmtree(prepared, ignore_errors=True)
            raise
        self.delete_input(token, suffix=suffix)
        self.cleanup()
        result.update(
            {
                "preview": self._result_url(token, "preview"),
            }
        )
        return result

    def test_release(self, settings: dict, commit_context: dict[str, str]) -> dict:
        token = self._validate_token(str(settings["input_token"]))
        suffix = str(settings["input_suffix"])
        device_id = str(settings["device_id"])
        profile_key = str(settings["profile"])
        transport = str(settings.get("transport", "custom"))
        stock_direct = transport == "stock_direct"
        prepared = self.prepared_root / uuid4().hex
        context = dict(commit_context)

        def cancelled() -> bool:
            return not self.jobs.can_commit_item(
                context["job_id"],
                context["item_id"],
                context["worker_id"],
                context["idempotency_key"],
            )

        def validate_device() -> dict:
            current = self.devices.get(device_id)
            if current is None or not bool(current["enabled"]):
                raise ValueError("DEVICE-006 找不到可用測試裝置")
            if profile_key != str(current["panel_profile"]):
                raise ValueError("DEVICE-006 測試色盤與裝置面板 Profile 不相容")
            if stock_direct and str(current.get("delivery_mode") or "") != "stock_compat":
                raise ValueError("DEVICE-008 Stock 測試只能選擇 Stock 相容裝置")
            return dict(current)

        def prepare_preset() -> tuple[dict | None, str | None, bool | None, str | None]:
            """Prepare optional preset data without making the release depend on it."""

            if not bool(settings.get("save_preset")):
                return None, None, None, None
            try:
                configuration = dict(settings["configuration"])
                try:
                    existing = json.loads(str(self.settings.get("render.custom_photo_presets", "{}")))
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing = {}
                if not isinstance(existing, dict):
                    existing = {}
                preset_id = "custom-" + sha256(context["idempotency_key"].encode("utf-8")).hexdigest()[:10]
                candidate = {
                    "id": preset_id,
                    "label": str(settings.get("preset_label", "測試後儲存")).strip()[:80] or "測試後儲存",
                    "source_preset": configuration["preset"],
                    "options": configuration["overrides"],
                    "palette": configuration["palette"],
                }
                stored = existing.get(preset_id)
                if isinstance(stored, dict):
                    return dict(stored), None, True, None
                existing[preset_id] = candidate
                encoded = json.dumps(existing, ensure_ascii=False, separators=(",", ":"))
                if len(encoded) > 50_000:
                    return candidate, None, False, "RENDER-PRESET-SIZE"
                return candidate, encoded, False, None
            except Exception:
                return None, None, False, "RENDER-PRESET-VALIDATION"

        def record_preset_warning(error_code: str, error_type: str) -> None:
            try:
                self.jobs.add_event(
                    context["job_id"],
                    "preset_warning",
                    "Test Release 已完成，但 Preset 未儲存",
                    {
                        "error_code": error_code[:64],
                        "error_type": error_type[:80],
                    },
                )
            except Exception:  # noqa: S110 -- release success cannot depend on warning I/O
                # Optional warning persistence must not fail a successful release.
                pass

        try:
            if cancelled():
                raise ProcessCallError("test release commit lease is no longer valid")
            validate_device()
            idempotency_key = context["idempotency_key"]
            manifest = self.publisher.find_device_test_by_idempotency(idempotency_key)
            created_release = False
            if manifest is None:
                prepared_result = self.process_boundary.call(
                    _prepare_test_release_child,
                    timeout_seconds=float(settings.get("timeout_seconds", 30)),
                    kwargs={
                        "settings": settings,
                        "input_path": str(self.input_path(token, suffix=suffix)),
                        "prepared_path": str(prepared),
                    },
                    cancel_requested=cancelled,
                    process_name="inktime-render-child",
                )
                if cancelled():
                    raise ProcessCallError("test release commit lease is no longer valid")
                validate_device()
                manifest = self.publisher.publish_preencoded(
                    source_photo_id=str(settings.get("source_photo_id", "device-test-upload")),
                    payload_path=prepared / "payload.bin",
                    preview_path=prepared / "preview.png",
                    profile_key=profile_key,
                    dither=str(prepared_result["dither"]),
                    color_distance=str(prepared_result["color_distance"]),
                    dither_strength=float(prepared_result["dither_strength"]),
                    linear_light=bool(prepared_result["linear_light"]),
                    palette=list(prepared_result["palette"]),
                    palette_version=str(prepared_result["palette_version"]),
                    metadata={
                        "idempotency_key": idempotency_key,
                        "preset": prepared_result["preset"],
                        "source_preset": prepared_result["source_preset"],
                        "pipeline": prepared_result["pipeline"],
                        "source_size": prepared_result["source_size"],
                        "source_sha256": str(settings.get("photo_sha", "")),
                        "caption": prepared_result.get("caption", ""),
                        "font_reference": prepared_result.get("font_reference", ""),
                        "transport": transport,
                        "stock_direct": stock_direct,
                        "stock_direct_device_id": device_id if stock_direct else None,
                        "server_rendered": True,
                    },
                )
                created_release = True
            if cancelled():
                if created_release:
                    self.publisher.discard_unassigned_device_test(
                        str(manifest["release_id"]), idempotency_key
                    )
                raise ProcessCallError("test release commit lease is no longer valid")
            if not created_release:
                validate_device()
            preset, preset_payload, preset_saved, preset_error = prepare_preset()
            if preset_payload is not None:
                try:
                    self.settings.update(
                        "render.custom_photo_presets",
                        preset_payload,
                        changed_by=str(settings.get("created_by", "system")),
                        source_ip="background-job",
                    )
                    preset_saved = True
                except Exception as exc:
                    preset_error = "RENDER-PRESET-WRITE"
                    record_preset_warning(preset_error, type(exc).__name__)
            elif preset_error is not None:
                record_preset_warning(preset_error, "PresetValidation")
            self.delete_input(token, suffix=suffix)
            if stock_direct:
                payload_entry = payload_entry_from_manifest(manifest)
                return {
                    "release_id": manifest["release_id"],
                    "file_name": str(payload_entry["name"]),
                    "release_kind": "device_test",
                    "transport": "stock_direct",
                    "device_id": device_id,
                    "source_sha256": str(settings.get("photo_sha", "")),
                    "stock_direct": True,
                    "stock_touches_custom_queue": False,
                    "stock_touches_custom_ack": False,
                    "server_rendered": True,
                    "stage": "stock_test_release_completed",
                }
            assignment = DeviceTestReleaseStore(self.release_dir).assign(
                device_id,
                manifest["release_id"],
                profile_key=profile_key,
                delivery=str(settings["delivery"]),
                one_time=bool(settings["one_time"]),
                restore_formal=bool(settings["restore_formal"]),
            )
            return {
                "release_id": manifest["release_id"],
                "release_kind": "device_test",
                "device_id": device_id,
                "delivery": assignment["delivery"],
                "one_time": assignment["one_time"],
                "restore_formal": assignment["restore_formal"],
                "formal_schedule_overwritten": False,
                "server_rendered": True,
                "saved_preset": preset if preset_saved else None,
                "preset_saved": preset_saved,
                "preset_error": preset_error,
                "stage": "device_test_completed",
            }
        finally:
            shutil.rmtree(prepared, ignore_errors=True)

    def cleanup(self) -> int:
        cutoff = datetime.now(timezone.utc) - self.retention
        removed = 0
        input_files = [path for path in self.input_root.iterdir() if path.is_file()]
        prepared_files = list(self.prepared_root.glob("*/*"))
        for path in input_files + list(self.result_root.glob("*/*")) + prepared_files:
            try:
                modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if modified_at < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        for directory in self.prepared_root.iterdir():
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        inputs: list[tuple[Path, float, int]] = []
        for path in self.input_root.iterdir():
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            inputs.append((path, stat.st_mtime, stat.st_size))
        inputs.sort(key=lambda item: item[1])
        input_total = sum(item[2] for item in inputs)
        while len(inputs) > 128 or input_total > self.max_result_bytes:
            path, _modified, size = inputs.pop(0)
            path.unlink(missing_ok=True)
            input_total -= size
            removed += 1
        entries: list[tuple[Path, float, int]] = []
        for directory in self.result_root.iterdir():
            if not directory.is_dir():
                continue
            files = list(directory.iterdir())
            try:
                result_modified = max((path.stat().st_mtime for path in files), default=0)
                size = sum(path.stat().st_size for path in files)
            except OSError:
                continue
            entries.append((directory, result_modified, size))
        entries.sort(key=lambda item: item[1])
        total = sum(item[2] for item in entries)
        while len(entries) > self.max_result_entries or total > self.max_result_bytes:
            directory, _modified, size = entries.pop(0)
            for path in directory.iterdir():
                path.unlink(missing_ok=True)
            directory.rmdir()
            total -= size
            removed += 1
        return removed

    def observability(self) -> dict[str, int]:
        return dict(self._metrics)

    def record_compare_cache(self, hit: bool) -> None:
        key = "compare_cache_hit" if hit else "compare_cache_miss"
        self._metrics[key] += 1
