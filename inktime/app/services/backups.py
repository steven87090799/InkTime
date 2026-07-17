from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import zipfile

from inktime import __version__
from inktime.app.db import Database


class BackupService:
    def __init__(self, database: Database, backup_dir: Path) -> None:
        self.database = database
        self.backup_dir = backup_dir.resolve()
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        temporary_db = self.backup_dir / f".inktime-{stamp}.sqlite3"
        archive = self.backup_dir / f"inktime-backup-{stamp}.zip"
        source = sqlite3.connect(self.database.path)
        target = sqlite3.connect(temporary_db)
        try:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("BACKUP-001 備份資料庫完整性檢查失敗")
        finally:
            target.close()
            source.close()
        manifest = {
            "schema_version": 1,
            "application_version": __version__,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "includes": ["資料庫", "一般設定", "加密敏感設定", "裝置設定", "發布中繼資料"],
            "excludes": ["原始照片", "縮圖快取", "記錄檔"],
        }
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(temporary_db, "inktime.sqlite3")
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        temporary_db.unlink(missing_ok=True)
        return archive

    def list(self):
        return sorted(self.backup_dir.glob("inktime-backup-*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)

    def validate(self, archive: Path) -> dict:
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            if names != {"inktime.sqlite3", "manifest.json"}:
                raise ValueError("BACKUP-002 備份內容不完整或含有未預期檔案")
            manifest = json.loads(bundle.read("manifest.json"))
            if manifest.get("schema_version") != 1:
                raise ValueError("BACKUP-002 不支援的備份版本")
        return manifest

    def enforce_retention(self, keep: int) -> int:
        removed = 0
        for path in self.list()[max(1, keep):]:
            path.unlink()
            removed += 1
        return removed
