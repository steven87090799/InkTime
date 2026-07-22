from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import re

from PIL import Image

from .palette import DisplayProfile, encode_image, get_display_profile


FOUR_COLORS = ((0, 0, 0), (255, 255, 255), (220, 30, 30), (245, 190, 25))


def _nearest_color(pixel) -> int:
    return min(
        range(4),
        key=lambda index: sum(
            (int(pixel[channel]) - FOUR_COLORS[index][channel]) ** 2 for channel in range(3)
        ),
    )


def pack_four_color_2bpp(image: Image.Image) -> bytes:
    return encode_image(
        image,
        profile_key="safe_4c",
        dither="none",
        color_distance="rgb",
        strength=0,
    ).payload


class AtomicReleasePublisher:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(
        self,
        images: list[tuple[str, Image.Image]],
        *,
        profile_key: str = "safe_4c",
        dither: str = "floyd_steinberg",
        color_distance: str = "oklab",
        dither_strength: float = 1.0,
        width: int = 480,
        height: int = 800,
        orientation: str = "portrait",
        profile_override: DisplayProfile | None = None,
        linear_light: bool = False,
        protected_mask: Image.Image | None = None,
        activate: bool = True,
        release_kind: str = "formal",
        metadata: dict | None = None,
    ) -> dict:
        if not images:
            raise ValueError("RENDER-001 至少需要一張圖片")
        if orientation not in {"portrait", "landscape"}:
            raise ValueError("RENDER-005 不支援的相框方向")
        release_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        temporary = self.root / f".{release_id}.tmp"
        final = self.root / release_id
        temporary.mkdir(mode=0o750)
        profile = profile_override or get_display_profile(profile_key)
        if profile.key != profile_key:
            raise ValueError("RENDER-006 自訂色盤與面板 Profile 不一致")
        if release_kind not in {"formal", "device_test"}:
            raise ValueError("RENDER-008 Release 類型不合法")
        effective_strength = (
            1.0 if dither in {"gooddisplay", "photo_smooth"} else float(dither_strength)
        )
        effective_color_distance = (
            "rgb" if dither in {"gooddisplay", "photo_smooth"} else color_distance
        )
        files = []
        output_palette = profile.colors
        try:
            for index, (photo_id, source) in enumerate(images, 1):
                rendered = source.convert("RGB")
                if rendered.size != (width, height):
                    raise ValueError(f"RENDER-002 圖片尺寸必須是 {width}×{height}")
                encoded = encode_image(
                    rendered,
                    profile_key=profile_key,
                    dither=dither,
                    color_distance=effective_color_distance,
                    strength=effective_strength,
                    linear_light=linear_light,
                    protected_mask=protected_mask,
                    profile=profile,
                )
                payload = encoded.payload
                output_palette = encoded.palette
                expected = width * height // (4 if profile.pixel_format == "2bpp" else 2)
                if len(payload) != expected:
                    raise ValueError("RENDER-002 索引影像檔案大小驗證失敗")
                filename = f"photo_{index}.bin"
                preview = f"preview_{index}.png"
                (temporary / filename).write_bytes(payload)
                encoded.preview.save(temporary / preview, "PNG")
                files.append(
                    {
                        "name": filename,
                        "size": len(payload),
                        "sha256": sha256(payload).hexdigest(),
                        "source_photo_id": photo_id,
                        "preview": preview,
                    }
                )
            manifest = {
                "schema_version": 1 if profile.pixel_format == "2bpp" else 2,
                "release_id": release_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "display_type": profile.display_type,
                "render_profile": profile.key,
                "panel_profile": profile.panel_profile,
                "palette_version": profile.palette_version,
                "release_kind": release_kind,
                "width": width,
                "height": height,
                "pixel_format": profile.pixel_format,
                "orientation": orientation,
                "dither": dither,
                "dither_strength": effective_strength,
                "color_distance": effective_color_distance,
                "palette": [
                    {"code": color.code, "name": color.name, "rgb": list(color.rgb)}
                    for color in output_palette
                ],
                "files": files,
            }
            if metadata:
                manifest["render_options"] = metadata
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            (temporary / "manifest.json").write_bytes(manifest_bytes)
            for path in temporary.iterdir():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            temporary.replace(final)
            if activate:
                pointer_tmp = self.root / ".latest.tmp"
                pointer_tmp.write_text(release_id, encoding="utf-8")
                pointer_tmp.replace(self.root / "latest")
                profile_pointer_tmp = self.root / f".latest.{profile.key}.tmp"
                profile_pointer_tmp.write_text(release_id, encoding="utf-8")
                profile_pointer_tmp.replace(self.root / f"latest.{profile.key}")
            return manifest
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def list(self) -> list[dict]:
        releases = []
        for manifest_path in self.root.glob("*/manifest.json"):
            try:
                releases.append(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(releases, key=lambda item: item["created_at"], reverse=True)

    def rollback(self, release_id: str) -> None:
        target = self.root / release_id / "manifest.json"
        if not target.is_file() or target.parent.parent != self.root:
            raise KeyError(release_id)
        temporary = self.root / ".latest.tmp"
        temporary.write_text(release_id, encoding="utf-8")
        temporary.replace(self.root / "latest")
        manifest = json.loads(target.read_text(encoding="utf-8"))
        profile_key = str(manifest.get("render_profile", "safe_4c"))
        get_display_profile(profile_key)
        profile_temporary = self.root / f".latest.{profile_key}.tmp"
        profile_temporary.write_text(release_id, encoding="utf-8")
        profile_temporary.replace(self.root / f"latest.{profile_key}")


class DeviceTestReleaseStore:
    """One request-local test release assignment per device, outside formal pointers."""

    _DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

    def __init__(self, release_root: Path) -> None:
        self.release_root = release_root.resolve()
        self.root = self.release_root / ".device-tests"
        self.root.mkdir(mode=0o750, parents=True, exist_ok=True)

    def _path(self, device_id: str) -> Path:
        if not self._DEVICE_ID.fullmatch(device_id):
            raise ValueError("DEVICE-006 裝置識別碼不合法")
        return self.root / f"{device_id}.json"

    def assign(
        self,
        device_id: str,
        release_id: str,
        *,
        profile_key: str,
        delivery: str,
        one_time: bool,
        restore_formal: bool,
    ) -> dict:
        if delivery not in {"immediate", "next_wake"}:
            raise ValueError("DEVICE-006 測試傳送時機不合法")
        manifest_path = self.release_root / release_id / "manifest.json"
        if not manifest_path.is_file() or manifest_path.parent.parent != self.release_root:
            raise ValueError("DEVICE-006 測試 Release 不存在")
        assignment = {
            "device_id": device_id,
            "release_id": release_id,
            "profile_key": profile_key,
            "delivery": delivery,
            "one_time": bool(one_time),
            "restore_formal": bool(restore_formal),
            "status": "active",
            "assigned_at": datetime.now(timezone.utc).isoformat(),
        }
        path = self._path(device_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(assignment, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return assignment

    def active(self, device_id: str, profile_key: str) -> dict | None:
        path = self._path(device_id)
        try:
            assignment = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if assignment.get("status") != "active" or assignment.get("profile_key") != profile_key:
            return None
        manifest_path = self.release_root / str(assignment.get("release_id", "")) / "manifest.json"
        if not manifest_path.is_file() or manifest_path.parent.parent != self.release_root:
            return None
        return assignment

    def mark_downloaded(self, device_id: str, release_id: str) -> None:
        path = self._path(device_id)
        try:
            assignment = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if assignment.get("release_id") != release_id or assignment.get("status") != "active":
            return
        if assignment.get("one_time") or assignment.get("restore_formal"):
            assignment["status"] = "consumed"
            assignment["consumed_at"] = datetime.now(timezone.utc).isoformat()
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(assignment, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
