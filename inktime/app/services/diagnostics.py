from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import zipfile

import psutil

from inktime import __version__
from inktime.app.core.security import redact
from inktime.app.db import Database
from inktime.app.domain.rendering.fonts import (
    BUILTIN_FONTS,
    DEFAULT_FONT_ASSET_ROOT,
    SUPPORTED_FONT_SUFFIXES,
)


class DiagnosticsService:
    def __init__(
        self,
        database: Database,
        data_dir: Path,
        thumbnail_dir: Path,
        *,
        settings_repository=None,
    ) -> None:
        self.database = database
        self.data_dir = data_dir.resolve()
        self.thumbnail_dir = thumbnail_dir.resolve()
        self.settings_repository = settings_repository
        self.started_at = time.time()
        self.process = psutil.Process()
        self.process.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None)
        self._cache_bytes_value = 0
        self._cache_bytes_at = 0.0

    @staticmethod
    def _directory_size(root: Path) -> int:
        if not root.exists():
            return 0
        return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())

    @staticmethod
    def _read_text(path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    @classmethod
    def _cgroup_snapshot(cls) -> dict:
        memory_current = cls._read_text("/sys/fs/cgroup/memory.current")
        memory_max = cls._read_text("/sys/fs/cgroup/memory.max")
        cpu_max = cls._read_text("/sys/fs/cgroup/cpu.max")

        def number(value: str | None) -> int | None:
            return int(value) if value and value.isdigit() else None

        return {
            "memory_current": number(memory_current),
            "memory_max": number(memory_max),
            "cpu_max": cpu_max,
        }

    def _cached_directory_size(self) -> tuple[int, bool]:
        ttl = (
            int(self.settings_repository.get("system.diagnostics_cache_seconds", 300))
            if self.settings_repository
            else 300
        )
        now = time.monotonic()
        refreshed = self._cache_bytes_at == 0 or now - self._cache_bytes_at >= max(30, ttl)
        if refreshed:
            self._cache_bytes_value = self._directory_size(self.thumbnail_dir)
            self._cache_bytes_at = now
        return self._cache_bytes_value, not refreshed

    def snapshot(self) -> dict:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage(self.data_dir)
        process_memory = self.process.memory_info()
        cache_bytes, cache_cached = self._cached_directory_size()
        wal = Path(str(self.database.path) + "-wal")
        with self.database.session() as connection:
            queue = connection.execute(
                "SELECT COUNT(*) FROM job_items WHERE status IN ('pending','running')"
            ).fetchone()[0]
            workers = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('running','retrying','pausing') AND heartbeat_at IS NOT NULL"
            ).fetchone()[0]
            libraries = connection.execute("SELECT root_path FROM libraries WHERE enabled=1").fetchall()
            providers = connection.execute("SELECT COUNT(*) FROM providers WHERE enabled=1").fetchone()[0]
        revision = os.environ.get("INKTIME_GIT_REVISION", "unknown")
        if revision == "unknown":
            git = shutil.which("git")
            try:
                if git:
                    revision = subprocess.run(  # noqa: S603 -- executable resolved with shutil.which; arguments are constant
                        [git, "rev-parse", "--short", "HEAD"],
                        capture_output=True,
                        text=True,
                        timeout=2,
                        check=True,
                    ).stdout.strip()
            except Exception:
                revision = "unknown"
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cpu_percent": psutil.cpu_percent(interval=None),
            "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else [],
            "memory": {"used": memory.used, "total": memory.total, "percent": memory.percent},
            "swap": {"used": swap.used, "total": swap.total, "percent": swap.percent},
            "process": {
                "rss": process_memory.rss,
                "vms": process_memory.vms,
                "cpu_percent": self.process.cpu_percent(interval=None),
                "threads": self.process.num_threads(),
                "open_files": len(self.process.open_files()),
            },
            "cgroup": self._cgroup_snapshot(),
            "disk": {"used": disk.used, "total": disk.total, "free": disk.free, "percent": disk.percent},
            "database": {
                "bytes": self.database.path.stat().st_size if self.database.path.exists() else 0,
                "wal_bytes": wal.stat().st_size if wal.exists() else 0,
                "integrity": self.database.integrity_check(),
            },
            "cache_bytes": cache_bytes,
            "cache_size_cached": cache_cached,
            "libraries": {
                "configured": len(libraries),
                "readable": sum(Path(row["root_path"]).is_dir() for row in libraries),
            },
            "providers_enabled": int(providers),
            "fonts": sum(
                (DEFAULT_FONT_ASSET_ROOT / font.filename).is_file() for font in BUILTIN_FONTS
            )
            + sum(
                path.is_file() and path.suffix.lower() in SUPPORTED_FONT_SUFFIXES
                for path in (self.data_dir / "fonts").glob("*")
            ),
            "release_latest": (self.data_dir / "releases" / "latest").exists(),
            "last_backup": next(
                (
                    path.name
                    for path in sorted(
                        (self.data_dir / "backups").glob("inktime-backup-*.zip"),
                        key=lambda item: item.stat().st_mtime,
                        reverse=True,
                    )
                ),
                None,
            ),
            "docker": Path("/.dockerenv").exists(),
            "worker_count": workers,
            "queue_length": queue,
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "application_version": __version__,
            "git_revision": revision,
            "build_time": os.environ.get("INKTIME_BUILD_TIME", "unknown"),
            "uptime_seconds": int(time.time() - self.started_at),
            "runtime_profile": {
                "analysis_concurrency": int(
                    self.settings_repository.get("analysis.concurrency", 1)
                    if self.settings_repository
                    else 1
                ),
                "queue_multiplier": int(
                    self.settings_repository.get("worker.queue_multiplier", 1)
                    if self.settings_repository
                    else 1
                ),
                "worker_poll_seconds": float(
                    self.settings_repository.get("worker.poll_seconds", 15)
                    if self.settings_repository
                    else 15
                ),
            },
        }

    def bundle(self) -> BytesIO:
        output = BytesIO()
        snapshot = redact(self.snapshot())
        # 診斷包不包含設定值、照片路徑、GPS、Cookie、Session 或任何 Secret。
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("diagnostics.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
            bundle.writestr(
                "README.txt",
                "此診斷包不包含 API Key、Token、密碼、Session、Cookie、精確私人路徑、GPS 或原始照片。\n",
            )
        output.seek(0)
        return output
