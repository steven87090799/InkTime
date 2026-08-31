from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import re
import tempfile
import time

from PIL import Image

from inktime.app.core.locks import fcntl
from inktime.app.domain.analysis.plan import AI_IMAGE_JPEG_QUALITY
from inktime.app.domain.photos.formats import (
    DERIVATIVE_FORMAT, DERIVATIVE_SIZES, DERIVATIVE_VERSION, ImageSourceError, load_rgb,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ThumbnailCache:
    ALLOWED_SIZES = DERIVATIVE_SIZES
    LOCK_SHARDS = 256

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_root = self.root / ".locks"
        self.lock_root.mkdir(mode=0o700, exist_ok=True)
        self.lock_root.chmod(0o700)
        self.cleanup_lock_path = self.lock_root / "cleanup.lock"

    @contextmanager
    def _cleanup_lock(self):
        descriptor = os.open(self.cleanup_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            os.chmod(self.cleanup_lock_path, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
            yield acquired
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def _generation_lock(self, cache_key: str, *, blocking: bool = True):
        """Lock one cache shard for generation, use, or nonblocking cleanup."""

        shard = int(sha256(cache_key.encode("ascii")).hexdigest()[:8], 16) % self.LOCK_SHARDS
        shard_path = self.lock_root / f"shard-{shard:03d}.lock"
        shard_descriptor = os.open(shard_path, os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            os.chmod(shard_path, 0o600)
            try:
                fcntl.flock(shard_descriptor, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            acquired = True
            yield True
        finally:
            if acquired:
                fcntl.flock(shard_descriptor, fcntl.LOCK_UN)
            os.close(shard_descriptor)

    @staticmethod
    def _validate(path: Path, size: int) -> bool:
        try:
            with Image.open(path) as image:
                if image.format != "JPEG":
                    return False
                width, height = image.size
                if width <= 0 or height <= 0 or max(width, height) > size:
                    return False
                image.load()  # JPEG verify() alone does not detect truncated pixel data.
            return True
        except (OSError, ValueError, Image.DecompressionBombError):
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

    def _destination(self, content_hash: str, size: int) -> tuple[str, Path]:
        if size not in self.ALLOWED_SIZES:
            raise ValueError("縮圖尺寸只支援 512、1024 或 1600px")
        normalized_hash = content_hash.casefold()
        if not _SHA256.fullmatch(normalized_hash):
            raise ValueError("THUMB-002 縮圖內容雜湊必須是 SHA-256")
        return normalized_hash, self.root / f"{normalized_hash}-{DERIVATIVE_VERSION}-{size}.jpg"

    def _get_or_create_locked(self, source: Path, normalized_hash: str, destination: Path, size: int) -> Path:
        temporary: Path | None = None
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
            with load_rgb(source, size) as image:
                image.save(temporary, format=DERIVATIVE_FORMAT, quality=AI_IMAGE_JPEG_QUALITY, optimize=True)
            if self._content_sha256(source) != normalized_hash:
                raise ImageSourceError("THUMB-004", "原始照片內容已在縮圖建立期間改變", 409)
            if not self._validate(temporary, size):
                raise ImageSourceError("THUMB-003", "縮圖格式或尺寸驗證失敗", 503)
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

    def get_or_create(self, source: Path, content_hash: str, size: int) -> Path:
        normalized_hash, destination = self._destination(content_hash, size)
        with self._generation_lock(destination.stem) as acquired:
            assert acquired
            return self._get_or_create_locked(source, normalized_hash, destination, size)

    @contextmanager
    def acquire_for_use(self, source: Path, content_hash: str, size: int):
        """Keep this cache shard alive until the caller has finished reading it."""
        normalized_hash, destination = self._destination(content_hash, size)
        with self._generation_lock(destination.stem) as acquired:
            assert acquired
            yield self._get_or_create_locked(source, normalized_hash, destination, size)

    def size_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.glob("*.jpg") if path.is_file())

    def clear(self) -> int:
        removed = 0
        for path in self.root.glob("*.jpg"):
            if path.is_file():
                path.unlink()
                removed += 1
        return removed

    def inventory(self, *, limit: int | None = None) -> list[tuple[Path, int, float, int, str]]:
        """Read cache metadata once so callers can bound their DB lookup."""
        entries: list[tuple[Path, int, float, int, str]] = []
        maximum = None if limit is None else max(1, int(limit))
        for path in self.root.glob("*.jpg"):
            if not path.is_file():
                continue
            stat = path.stat()
            stem = path.stem.split("-", 1)[0].casefold()
            size_text = path.stem.rsplit("-", 1)[-1]
            size = int(size_text) if size_text.isdigit() else 0
            entries.append((path, stat.st_size, stat.st_atime, size, stem))
            if maximum is not None and len(entries) >= maximum:
                break
        return entries

    def estimate_cleanup(self, *, max_bytes: int, retention_days: int, active_hashes: set[str]) -> dict:
        candidates = self._cleanup_candidates(max_bytes, retention_days, active_hashes)
        return {
            "files": len(candidates),
            "bytes": sum(path.stat().st_size for path in candidates if path.exists()),
        }

    def cleanup(
        self,
        *,
        max_bytes: int,
        retention_days: int,
        active_hashes: set[str],
        max_files_per_run: int = 500,
        max_bytes_per_run: int = 512 * 1024 * 1024,
        inventory: list[tuple[Path, int, float, int, str]] | None = None,
    ) -> dict:
        with self._cleanup_lock() as acquired:
            if not acquired:
                return {"files": 0, "bytes": 0, "skipped": "cleanup_locked"}
            candidates = self._cleanup_candidates(
                max_bytes, retention_days, active_hashes, inventory=inventory
            )
            removed = 0
            released = 0
            for path in candidates:
                if removed >= max(1, int(max_files_per_run)) or released >= max(1, int(max_bytes_per_run)):
                    break
                try:
                    cache_key = path.stem
                    with self._generation_lock(cache_key, blocking=False) as acquired:
                        if not acquired:
                            continue
                        released += path.stat().st_size
                        path.unlink()
                        removed += 1
                except FileNotFoundError:
                    continue
            return {"files": removed, "bytes": released}

    def _cleanup_candidates(
        self,
        max_bytes: int,
        retention_days: int,
        active_hashes: set[str],
        *,
        inventory: list[tuple[Path, int, float, int, str]] | None = None,
    ) -> list[Path]:
        max_bytes = max(0, int(max_bytes))
        retention_seconds = max(0, int(retention_days)) * 86400
        now = time.time()
        entries: list[tuple[Path, int, float, int, bool]] = []
        for path, bytes_used, accessed, size, stem in (
            inventory if inventory is not None else self.inventory()
        ):
            orphan = (
                not _SHA256.fullmatch(stem)
                or stem not in active_hashes
                or size not in self.ALLOWED_SIZES
                or not self._validate(path, size)
            )
            entries.append((path, bytes_used, accessed, size, orphan))
        selected: list[Path] = []
        total = sum(bytes_used for _, bytes_used, _, _, _ in entries)
        # Always clear invalid/orphaned and expired files before evicting valid
        # cache entries.  Capacity eviction then drops the largest derivatives
        # first (1600, 1024, 512), preserving the fastest preview cache.
        for path, bytes_used, accessed, _size, orphan in entries:
            if orphan or (retention_seconds and now - accessed > retention_seconds):
                selected.append(path)
                total -= bytes_used
        for path, bytes_used, _accessed, _size, _orphan in sorted(
            entries, key=lambda entry: (-entry[3], entry[2])
        ):
            if total <= max_bytes:
                break
            if path not in selected:
                selected.append(path)
                total -= bytes_used
        return selected
