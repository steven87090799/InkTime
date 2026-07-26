from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from PIL import Image
from PIL import ImageOps

from inktime.app.domain.rendering import (
    DeviceTestReleaseStore,
    encode_image,
    palette_for_profile,
    render_photo,
)


class RenderWorkloadService:
    """Persist bounded private inputs/results for existing background Jobs."""

    def __init__(
        self, root: Path, publisher, devices, release_dir: Path, settings_repository
    ) -> None:
        self.root = root.resolve()
        self.input_root = self.root / "inputs"
        self.result_root = self.root / "results"
        self.input_root.mkdir(parents=True, exist_ok=True)
        self.result_root.mkdir(parents=True, exist_ok=True)
        self.publisher = publisher
        self.devices = devices
        self.release_dir = release_dir.resolve()
        self.settings = settings_repository
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
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            image.save(temporary, "PNG", optimize=True)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def save_input(self, image: Image.Image) -> tuple[str, str]:
        token = uuid4().hex
        destination = self.input_root / f"{token}.png"
        self._atomic_png(destination, image.convert("RGB"))
        return token, sha256(destination.read_bytes()).hexdigest()

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

    @staticmethod
    def _atomic_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
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
        counts = Counter(image.convert("RGB").getdata())
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
        fingerprint = json.dumps(
            dict(settings["cache_fingerprint"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        token = sha256(fingerprint.encode("utf-8")).hexdigest()[:32]
        cached = self._cached_compare(token)
        if cached is not None:
            (self.input_root / f"{input_token}.png").unlink(missing_ok=True)
            cached["cache_hit"] = True
            return cached
        image = self._open_input(input_token)
        configuration = dict(settings["configuration"])
        result = render_photo(
            image,
            profile_key=str(settings["profile"]),
            preset=str(configuration["preset"]),
            overrides=dict(configuration["overrides"]),
            fit=str(settings["fit"]),
            palette_rgb=configuration.get("palette_rgb"),
            palette_lab=configuration.get("palette_lab"),
            palette_version=str(configuration["palette_version"]),
            text_regions=list(configuration.get("text_regions", [])),
            face_regions=list(configuration.get("face_regions", [])),
        )
        started = datetime.now(timezone.utc)
        legacy = encode_image(
            result.source,
            profile_key=str(settings["profile"]),
            dither="gooddisplay",
            color_distance="rgb",
            strength=1.0,
        )
        legacy_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        for name, output in {
            "original": result.source,
            "legacy": legacy.preview,
            "new": result.encoded.preview,
        }.items():
            self._atomic_png(self.result_path(token, name), output)
        response = {
            "original": self._result_url(token, "original"),
            "legacy": self._result_url(token, "legacy"),
            "new": self._result_url(token, "new"),
            "source_size": str(settings["source_size"]),
            "payload_bytes": len(result.encoded.payload),
            "render_ms": result.render_ms,
            "legacy_render_ms": legacy_ms,
            "preset": str(configuration["requested_preset"]),
            "source_preset": result.preset,
            "dither": result.options["dither"],
            "color_distance": result.options["color_distance"],
            "linear_light": bool(result.options.get("linear_light")),
            "palette": self._palette_statistics(result.encoded.preview, result.encoded.palette),
            "publish_source": "server_original_upload_only",
            "model": "disabled",
            "cache_hit": False,
            "stage": "preview_completed",
        }
        self._atomic_json(self.result_root / token / "result.json", response)
        (self.input_root / f"{input_token}.png").unlink(missing_ok=True)
        self.cleanup()
        return response

    def simulate(self, settings: dict) -> dict:
        token = self._validate_token(str(settings["input_token"]))
        image = self._open_input(token)
        size = (480, 800)
        if str(settings["fit"]) == "cover":
            canvas = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
        else:
            fitted = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", size, "white")
            canvas.paste(
                fitted,
                ((canvas.width - fitted.width) // 2, (canvas.height - fitted.height) // 2),
            )
        encoded = encode_image(
            canvas,
            profile_key=str(settings["profile"]),
            dither=str(settings["dither"]),
            color_distance=str(settings["color_distance"]),
            strength=float(settings["strength"]),
        )
        self._atomic_png(self.result_path(token, "preview"), encoded.preview)
        (self.input_root / f"{token}.png").unlink(missing_ok=True)
        self.cleanup()
        return {
            "preview": self._result_url(token, "preview"),
            "source_size": str(settings["source_size"]),
            "payload_bytes": len(encoded.payload),
            "profile": str(settings["profile"]),
            "dither": str(settings["dither"]),
            "stage": "preview_completed",
        }

    def test_release(self, settings: dict) -> dict:
        token = self._validate_token(str(settings["input_token"]))
        device_id = str(settings["device_id"])
        device = self.devices.get(device_id)
        if device is None or not bool(device["enabled"]):
            raise ValueError("DEVICE-006 找不到可用測試裝置")
        profile_key = str(settings["profile"])
        if profile_key != str(device["panel_profile"]):
            raise ValueError("DEVICE-006 測試色盤與裝置面板 Profile 不相容")
        configuration = dict(settings["configuration"])
        result = render_photo(
            self._open_input(token),
            profile_key=profile_key,
            preset=str(configuration["preset"]),
            overrides=dict(configuration["overrides"]),
            fit=str(settings["fit"]),
            palette_rgb=configuration.get("palette_rgb"),
            palette_lab=configuration.get("palette_lab"),
            palette_version=str(configuration["palette_version"]),
            text_regions=list(configuration.get("text_regions", [])),
            face_regions=list(configuration.get("face_regions", [])),
        )
        profile = palette_for_profile(
            profile_key,
            rgb_values=configuration.get("palette_rgb"),
            lab_values=configuration.get("palette_lab"),
            palette_version=str(configuration["palette_version"]),
        )
        manifest = self.publisher.publish(
            [("device-test-upload", result.processed)],
            profile_key=profile_key,
            profile_override=profile,
            dither=str(result.options["dither"]),
            color_distance=str(result.options["color_distance"]),
            dither_strength=float(result.options["error_strength"]),
            linear_light=bool(result.options.get("linear_light")),
            protected_mask=result.protected_mask,
            activate=False,
            release_kind="device_test",
            metadata={
                "preset": configuration["requested_preset"],
                "source_preset": result.preset,
                "pipeline": result.options,
                "source_size": settings["source_size"],
                "server_rendered": True,
            },
        )
        assignment = DeviceTestReleaseStore(self.release_dir).assign(
            device_id,
            manifest["release_id"],
            profile_key=profile_key,
            delivery=str(settings["delivery"]),
            one_time=bool(settings["one_time"]),
            restore_formal=bool(settings["restore_formal"]),
        )
        saved_preset = None
        if bool(settings.get("save_preset")):
            try:
                existing = json.loads(
                    str(self.settings.get("render.custom_photo_presets", "{}"))
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                existing = {}
            if not isinstance(existing, dict):
                existing = {}
            preset_id = f"custom-{uuid4().hex[:10]}"
            saved_preset = {
                "id": preset_id,
                "label": str(settings.get("preset_label", "測試後儲存")).strip()[:80]
                or "測試後儲存",
                "source_preset": configuration["preset"],
                "options": configuration["overrides"],
                "palette": configuration["palette"],
            }
            existing[preset_id] = saved_preset
            encoded = json.dumps(existing, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) > 50_000:
                raise ValueError("RENDER-007 自訂 Preset 總資料量超過 50000 字元")
            self.settings.update(
                "render.custom_photo_presets",
                encoded,
                changed_by=str(settings.get("created_by", "system")),
                source_ip="background-job",
            )
        (self.input_root / f"{token}.png").unlink(missing_ok=True)
        return {
            "release_id": manifest["release_id"],
            "release_kind": "device_test",
            "device_id": device_id,
            "delivery": assignment["delivery"],
            "one_time": assignment["one_time"],
            "restore_formal": assignment["restore_formal"],
            "formal_schedule_overwritten": False,
            "server_rendered": True,
            "saved_preset": saved_preset,
            "stage": "device_test_completed",
        }

    def cleanup(self) -> int:
        cutoff = datetime.now(timezone.utc) - self.retention
        removed = 0
        for path in list(self.input_root.glob("*.png")) + list(self.result_root.glob("*/*")):
            try:
                modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if modified_at < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        inputs: list[tuple[Path, float, int]] = []
        for path in self.input_root.glob("*.png"):
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
