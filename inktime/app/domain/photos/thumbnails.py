from __future__ import annotations

from pathlib import Path
import shutil

from PIL import Image, ImageOps


class ThumbnailCache:
    ALLOWED_SIZES = {512, 1024, 1600}

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def get_or_create(self, source: Path, content_hash: str, size: int) -> Path:
        if size not in self.ALLOWED_SIZES:
            raise ValueError("縮圖尺寸只支援 512、1024 或 1600px")
        destination = self.root / f"{content_hash}-{size}.jpg"
        if destination.is_file():
            return destination
        temporary = self.root / f".{content_hash}-{size}.tmp"
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            image.save(temporary, format="JPEG", quality=88, optimize=True)
        temporary.replace(destination)
        return destination

    def size_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.glob("*.jpg") if path.is_file())

    def clear(self) -> int:
        removed = 0
        for path in self.root.glob("*.jpg"):
            if path.is_file():
                path.unlink()
                removed += 1
        return removed
