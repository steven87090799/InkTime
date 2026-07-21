from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil

from PIL import Image

from .palette import encode_image, get_display_profile


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
    ) -> dict:
        if not images:
            raise ValueError("RENDER-001 至少需要一張圖片")
        if orientation not in {"portrait", "landscape"}:
            raise ValueError("RENDER-005 不支援的相框方向")
        release_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        temporary = self.root / f".{release_id}.tmp"
        final = self.root / release_id
        temporary.mkdir(mode=0o750)
        profile = get_display_profile(profile_key)
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
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            (temporary / "manifest.json").write_bytes(manifest_bytes)
            for path in temporary.iterdir():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            temporary.replace(final)
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
