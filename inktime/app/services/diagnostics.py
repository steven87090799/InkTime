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


class DiagnosticsService:
    def __init__(self, database: Database, data_dir: Path, thumbnail_dir: Path) -> None:
        self.database = database
        self.data_dir = data_dir.resolve()
        self.thumbnail_dir = thumbnail_dir.resolve()
        self.started_at = time.time()

    @staticmethod
    def _directory_size(root: Path) -> int:
        if not root.exists():
            return 0
        return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())

    def snapshot(self) -> dict:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage(self.data_dir)
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
            "cpu_percent": psutil.cpu_percent(interval=0.05),
            "memory": {"used": memory.used, "total": memory.total, "percent": memory.percent},
            "swap": {"used": swap.used, "total": swap.total, "percent": swap.percent},
            "disk": {"used": disk.used, "total": disk.total, "free": disk.free, "percent": disk.percent},
            "database": {
                "bytes": self.database.path.stat().st_size if self.database.path.exists() else 0,
                "wal_bytes": wal.stat().st_size if wal.exists() else 0,
                "integrity": self.database.integrity_check(),
            },
            "cache_bytes": self._directory_size(self.thumbnail_dir),
            "libraries": {
                "configured": len(libraries),
                "readable": sum(Path(row["root_path"]).is_dir() for row in libraries),
            },
            "providers_enabled": int(providers),
            "fonts": len(list((self.data_dir / "fonts").glob("*"))),
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
