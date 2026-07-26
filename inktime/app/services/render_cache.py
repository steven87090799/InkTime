from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

from PIL import Image, UnidentifiedImageError


RENDERER_VERSION = "runtime-cache-v1"


class BoundedRenderCache:
    """Private, bounded PNG cache; it is never rooted in the Release directory."""

    def __init__(
        self,
        root: Path,
        *,
        max_entries: int = 256,
        max_bytes: int = 256 * 1024 * 1024,
        retention: timedelta = timedelta(days=7),
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024 * 1024, int(max_bytes))
        self.retention = retention
        self._lock = threading.Lock()
        self._metrics = {
            "hit": 0,
            "miss": 0,
            "write": 0,
            "corrupt": 0,
            "sync_count": 0,
            "sync_duration_ms": 0,
            "sync_duration_max_ms": 0,
            "preview_job_count": 0,
            "preview_job_duration_ms": 0,
            "preview_job_duration_max_ms": 0,
        }

    @staticmethod
    def fingerprint(values: dict[str, Any]) -> str:
        canonical = json.dumps(
            values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
            raise ValueError("invalid renderer cache key")
        return self.root / f"{key}.png"

    def get(self, fingerprint: dict[str, Any]) -> Image.Image | None:
        key = self.fingerprint(fingerprint)
        path = self._path(key)
        with self._lock:
            if not path.is_file():
                self._metrics["miss"] += 1
                return None
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < (
                    datetime.now(timezone.utc) - self.retention
                ):
                    path.unlink(missing_ok=True)
                    self._metrics["miss"] += 1
                    return None
                payload = path.read_bytes()
                with Image.open(BytesIO(payload)) as opened:
                    opened.verify()
                with Image.open(BytesIO(payload)) as opened:
                    image = opened.convert("RGB")
                os.utime(path, None)
            except (OSError, UnidentifiedImageError, ValueError):
                path.unlink(missing_ok=True)
                self._metrics["corrupt"] += 1
                self._metrics["miss"] += 1
                return None
            self._metrics["hit"] += 1
            return image

    def put(self, fingerprint: dict[str, Any], image: Image.Image) -> str:
        key = self.fingerprint(fingerprint)
        destination = self._path(key)
        with self._lock:
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{key}-", suffix=".tmp", dir=self.root
            )
            os.close(handle)
            temporary = Path(temporary_name)
            try:
                image.save(temporary, "PNG", optimize=True)
                os.replace(temporary, destination)
                self._metrics["write"] += 1
            finally:
                temporary.unlink(missing_ok=True)
            self._cleanup_locked()
        return key

    def _cleanup_locked(self) -> int:
        now = datetime.now(timezone.utc)
        entries: list[tuple[Path, float, int]] = []
        removed = 0
        for path in self.root.glob("*.png"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if datetime.fromtimestamp(stat.st_mtime, timezone.utc) < now - self.retention:
                path.unlink(missing_ok=True)
                removed += 1
            else:
                entries.append((path, stat.st_mtime, stat.st_size))
        entries.sort(key=lambda item: item[1])
        total = sum(item[2] for item in entries)
        while len(entries) > self.max_entries or total > self.max_bytes:
            path, _modified, size = entries.pop(0)
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed

    def cleanup(self) -> int:
        with self._lock:
            return self._cleanup_locked()

    def observability(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)

    def record_duration(self, duration_ms: int, *, background: bool) -> None:
        prefix = "preview_job" if background else "sync"
        bounded = max(0, min(int(duration_ms), 24 * 60 * 60 * 1000))
        with self._lock:
            self._metrics[f"{prefix}_count"] += 1
            self._metrics[f"{prefix}_duration_ms"] += bounded
            self._metrics[f"{prefix}_duration_max_ms"] = max(
                self._metrics[f"{prefix}_duration_max_ms"], bounded
            )
