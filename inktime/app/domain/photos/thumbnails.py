from __future__ import annotations

import fcntl
from hashlib import sha256
import os
from pathlib import Path
import re
import tempfile
import time

from PIL import Image, ImageOps


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ThumbnailCache:
    ALLOWED_SIZES = {512, 1024, 1600}

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate(path: Path, size: int) -> bool:
        try:
            with Image.open(path) as image:
                if image.format != "JPEG":
                    return False
                width, height = image.size
                if width <= 0 or height <= 0 or max(width, height) > size:
                    return False
                image.verify()
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _content_sha256(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def get_or_create(self, source: Path, content_hash: str, size: int) -> Path:
        if size not in self.ALLOWED_SIZES:
            raise ValueError("縮圖尺寸只支援 512、1024 或 1600px")
        normalized_hash = content_hash.casefold()
        if not _SHA256.fullmatch(normalized_hash):
            raise ValueError("THUMB-002 縮圖內容雜湊必須是 SHA-256")
        destination = self.root / f"{normalized_hash}-{size}.jpg"
        lock_path = self.root / f".{normalized_hash}-{size}.lock"
        temporary: Path | None = None
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if destination.is_file() and self._validate(destination, size):
                # Cache hits refresh recency for cleanup without rewriting the generated thumbnail.
                cached_stat = destination.stat()
                os.utime(destination, ns=(time.time_ns(), cached_stat.st_mtime_ns))
                return destination
            if destination.exists():
                destination.unlink()
            handle = tempfile.NamedTemporaryFile(
                dir=self.root,
                prefix=f".{normalized_hash}-{size}-",
                suffix=".tmp",
                delete=False,
            )
            temporary = Path(handle.name)
            handle.close()
            try:
                with Image.open(source) as opened:
                    image = ImageOps.exif_transpose(opened).convert("RGB")
                    image.thumbnail((size, size), Image.Resampling.LANCZOS)
                    image.save(temporary, format="JPEG", quality=88, optimize=True)
                if self._content_sha256(source) != normalized_hash:
                    raise OSError("THUMB-004 原始照片內容已在縮圖建立期間改變")
                if not self._validate(temporary, size):
                    raise OSError("THUMB-003 縮圖格式或尺寸驗證失敗")
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                self._fsync_directory(self.root)
                temporary = None
                os.utime(destination, None)
                return destination
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def size_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.glob("*.jpg") if path.is_file())

    def clear(self) -> int:
        removed = 0
        for path in self.root.glob("*.jpg"):
            if path.is_file():
                path.unlink()
                removed += 1
        return removed

    def estimate_cleanup(self, *, max_bytes: int, retention_days: int, active_hashes: set[str]) -> dict:
        candidates = self._cleanup_candidates(max_bytes, retention_days, active_hashes)
        return {"files": len(candidates), "bytes": sum(path.stat().st_size for path in candidates if path.exists())}

    def cleanup(self, *, max_bytes: int, retention_days: int, active_hashes: set[str]) -> dict:
        candidates = self._cleanup_candidates(max_bytes, retention_days, active_hashes)
        removed = 0
        released = 0
        for path in candidates:
            try:
                released += path.stat().st_size
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
        return {"files": removed, "bytes": released}

    def _cleanup_candidates(self, max_bytes: int, retention_days: int, active_hashes: set[str]) -> list[Path]:
        max_bytes = max(0, int(max_bytes))
        retention_seconds = max(0, int(retention_days)) * 86400
        now = time.time()
        entries: list[tuple[Path, int, float, bool]] = []
        for path in self.root.glob("*.jpg"):
            if not path.is_file():
                continue
            stem = path.stem.split("-", 1)[0].casefold()
            size_text = path.stem.rsplit("-", 1)[-1]
            size = int(size_text) if size_text.isdigit() else 0
            stat = path.stat()
            orphan = (
                not _SHA256.fullmatch(stem)
                or stem not in active_hashes
                or size not in self.ALLOWED_SIZES
                or not self._validate(path, size)
            )
            entries.append((path, stat.st_size, stat.st_atime, orphan))
        selected: list[Path] = []
        total = sum(size for _, size, _, _ in entries)
        for path, size, accessed, orphan in entries:
            if orphan or (retention_seconds and now - accessed > retention_seconds):
                selected.append(path)
                total -= size
        for path, size, _accessed, _orphan in sorted(entries, key=lambda entry: entry[2]):
            if total <= max_bytes:
                break
            if path not in selected:
                selected.append(path)
                total -= size
        return selected
