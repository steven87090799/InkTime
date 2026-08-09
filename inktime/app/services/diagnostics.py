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
    _WORKER_IDLE_POLL_CAP_SECONDS = 60.0
    _DATABASE_QUICK_CHECK_TTL_SECONDS = 24 * 60 * 60
    _BACKUP_INVENTORY_TTL_SECONDS = 6 * 60 * 60

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
        self._database_integrity_value = "unknown"
        self._database_integrity_at = 0.0
        self._backup_inventory_value: str | None = None
        self._backup_inventory_at = 0.0
        self._font_inventory_value = 0
        self._font_inventory_at = 0.0
        self._resolved_git_revision: str | None = None

    @staticmethod
    def _directory_size(root: Path) -> int:
        if not root.exists():
            return 0
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                # Cache/WAL maintenance may remove a file between discovery
                # and stat; diagnostics must remain best-effort.
                continue
        return total

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

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
            int(self.settings_repository.get("system.diagnostics_cache_seconds", 21600))
            if self.settings_repository
            else 21600
        )
        now = time.monotonic()
        refreshed = self._cache_bytes_at == 0 or now - self._cache_bytes_at >= max(30, ttl)
        if refreshed:
            self._cache_bytes_value = self._directory_size(self.thumbnail_dir)
            self._cache_bytes_at = now
        return self._cache_bytes_value, not refreshed

    def _cached_database_quick_check(self, *, force: bool = False) -> str:
        now = time.monotonic()
        if (
            force
            or
            self._database_integrity_at == 0
            or now - self._database_integrity_at >= self._DATABASE_QUICK_CHECK_TTL_SECONDS
        ):
            self._database_integrity_value = self.database.integrity_check()
            self._database_integrity_at = now
        return self._database_integrity_value

    def _cached_backup_inventory(self) -> str | None:
        now = time.monotonic()
        if (
            self._backup_inventory_at == 0
            or now - self._backup_inventory_at >= self._BACKUP_INVENTORY_TTL_SECONDS
        ):
            backup_dir = self.data_dir / "backups"
            try:
                candidates = sorted(
                    backup_dir.glob("inktime-backup-*.zip"),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                candidates = []
            self._backup_inventory_value = candidates[0].name if candidates else None
            self._backup_inventory_at = now
        return self._backup_inventory_value

    def _cached_font_inventory(self) -> int:
        now = time.monotonic()
        if self._font_inventory_at == 0 or now - self._font_inventory_at >= self._BACKUP_INVENTORY_TTL_SECONDS:
            self._font_inventory_value = sum(
                (DEFAULT_FONT_ASSET_ROOT / font.filename).is_file() for font in BUILTIN_FONTS
            ) + sum(
                path.is_file() and path.suffix.lower() in SUPPORTED_FONT_SUFFIXES
                for path in (self.data_dir / "fonts").glob("*")
            )
            self._font_inventory_at = now
        return self._font_inventory_value

    def snapshot(self, *, force_integrity: bool = False) -> dict:
        runtime_settings = (
            self.settings_repository.get_many(
                [
                    "system.diagnostics_cache_seconds",
                    "analysis.concurrency",
                    "worker.queue_multiplier",
                    "worker.poll_seconds",
                ],
                defaults={
                    "system.diagnostics_cache_seconds": 21600,
                    "analysis.concurrency": 1,
                    "worker.queue_multiplier": 1,
                    "worker.poll_seconds": 15,
                },
            )
            if self.settings_repository
            else {}
        )
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
        if revision == "unknown" and self._resolved_git_revision is None:
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
            self._resolved_git_revision = revision
        elif revision == "unknown" and self._resolved_git_revision is not None:
            revision = self._resolved_git_revision
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
                "bytes": self._file_size(self.database.path),
                "wal_bytes": self._file_size(wal),
                "integrity": self._cached_database_quick_check(force=force_integrity),
            },
            "cache_bytes": cache_bytes,
            "cache_size_cached": cache_cached,
            "libraries": {
                "configured": len(libraries),
                "readable": sum(Path(row["root_path"]).is_dir() for row in libraries),
            },
            "providers_enabled": int(providers),
            "fonts": self._cached_font_inventory(),
            "release_latest": (self.data_dir / "releases" / "latest").exists(),
            "last_backup": self._cached_backup_inventory(),
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
                    runtime_settings.get("analysis.concurrency", 1)
                ),
                "queue_multiplier": int(
                    runtime_settings.get("worker.queue_multiplier", 1)
                ),
                "worker_poll_seconds": float(
                    min(
                        self._WORKER_IDLE_POLL_CAP_SECONDS,
                        max(
                            1.0,
                            float(runtime_settings.get("worker.poll_seconds", 15)),
                        ),
                    )
                ),
            },
        }

    def bundle(self) -> BytesIO:
        output = BytesIO()
        snapshot = redact(self.snapshot(force_integrity=True))
        # 診斷包不包含設定值、照片路徑、GPS、Cookie、Session 或任何 Secret。
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("diagnostics.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
            bundle.writestr(
                "README.txt",
                "此診斷包不包含 API Key、Token、密碼、Session、Cookie、精確私人路徑、GPS 或原始照片。\n",
            )
        output.seek(0)
        return output
