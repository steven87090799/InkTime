from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import IO
import zipfile

from inktime import __version__
from inktime.app.db import Database
from inktime.app.db.migrations import MIGRATIONS, migrate


BACKUP_FORMAT_VERSION = 2
IMPORTANT_TABLES = (
    "photos",
    "photo_analysis",
    "photo_events",
    "jobs",
    "job_items",
    "scan_runs",
    "scan_missing_candidates",
    "releases",
    "device_events",
    "display_history",
)

_COUNT_SQL = {
    "photos": "SELECT COUNT(*) FROM photos",
    "photo_analysis": "SELECT COUNT(*) FROM photo_analysis",
    "photo_events": "SELECT COUNT(*) FROM photo_events",
    "jobs": "SELECT COUNT(*) FROM jobs",
    "job_items": "SELECT COUNT(*) FROM job_items",
    "scan_runs": "SELECT COUNT(*) FROM scan_runs",
    "scan_missing_candidates": "SELECT COUNT(*) FROM scan_missing_candidates",
    "releases": "SELECT COUNT(*) FROM releases",
    "device_events": "SELECT COUNT(*) FROM device_events",
    "display_history": "SELECT COUNT(*) FROM display_history",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stream_sha256(stream: IO[bytes]) -> tuple[str, int]:
    digest = sha256()
    total = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _database_counts(connection: sqlite3.Connection) -> dict[str, int]:
    existing = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return {
        table: int(connection.execute(_COUNT_SQL[table]).fetchone()[0])
        for table in IMPORTANT_TABLES
        if table in existing
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_database_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", ".writer.lock", ".migration.lock", ".runtime.lock"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


class BackupService:
    def __init__(self, database: Database, backup_dir: Path) -> None:
        self.database = database
        self.backup_dir = backup_dir.resolve()
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _copy_database(self, destination: Path, *, include_secrets: bool) -> dict[str, int]:
        source = sqlite3.connect(self.database.path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            if not include_secrets:
                target.execute("PRAGMA secure_delete = ON")
                target.execute("DELETE FROM secrets")
                target.commit()
                target.execute("VACUUM")
            integrity = target.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise RuntimeError("BACKUP-001 備份資料庫完整性檢查失敗")
            return _database_counts(target)
        finally:
            target.close()
            source.close()

    def _settings_export(self, database_path: Path, *, include_secrets: bool) -> bytes:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT key,category,value_json,value_type,requires_restart,updated_at
                FROM settings ORDER BY key
                """
            ).fetchall()
        finally:
            connection.close()
        return json.dumps(
            {
                "schema_version": 1,
                "exported_at": _utc_now(),
                "settings": [dict(row) for row in rows],
                "secrets_included": include_secrets,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

    def create(self, *, include_secrets: bool = False) -> Path:
        """建立原子、可驗證備份；預設不納入 API Key／Webhook Token。"""

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = self.backup_dir / f"inktime-backup-{stamp}.zip"
        db_handle = tempfile.NamedTemporaryFile(
            dir=self.backup_dir, prefix=".inktime-db-", suffix=".sqlite3", delete=False
        )
        temporary_db = Path(db_handle.name)
        db_handle.close()
        zip_handle = tempfile.NamedTemporaryFile(
            dir=self.backup_dir, prefix=".inktime-archive-", suffix=".zip", delete=False
        )
        temporary_archive = Path(zip_handle.name)
        zip_handle.close()
        try:
            counts = self._copy_database(temporary_db, include_secrets=include_secrets)
            settings = self._settings_export(
                temporary_db, include_secrets=include_secrets
            )
            with temporary_db.open("rb") as stream:
                database_hash, database_size = _stream_sha256(stream)
            settings_hash = sha256(settings).hexdigest()
            manifest = {
                "backup_format_version": BACKUP_FORMAT_VERSION,
                "application_version": __version__,
                "database_schema_version": self._schema_version(temporary_db),
                "created_at": _utc_now(),
                "secrets_policy": "included" if include_secrets else "excluded",
                "includes": [
                    "SQLite 資料庫",
                    "一般設定",
                    "照片分析結果",
                    "照片與排程狀態",
                    "顯示與發布歷史資料庫紀錄",
                ],
                "excludes": [
                    "原始照片",
                    "縮圖快取",
                    "已渲染 Release 檔案",
                    "記錄檔",
                    *([] if include_secrets else ["API Key", "Webhook Token"]),
                ],
                "files": {
                    "inktime.sqlite3": {
                        "sha256": database_hash,
                        "size": database_size,
                    },
                    "settings.json": {
                        "sha256": settings_hash,
                        "size": len(settings),
                    },
                },
                "important_table_counts": counts,
            }
            with zipfile.ZipFile(
                temporary_archive,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as bundle:
                bundle.write(temporary_db, "inktime.sqlite3")
                bundle.writestr("settings.json", settings)
                bundle.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )
            self.validate(temporary_archive)
            with temporary_archive.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary_archive, archive)
            _fsync_directory(self.backup_dir)
            return archive
        finally:
            temporary_db.unlink(missing_ok=True)
            temporary_archive.unlink(missing_ok=True)

    @staticmethod
    def _schema_version(path: Path) -> int:
        connection = sqlite3.connect(path)
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not exists:
                return 0
            return int(connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0])
        finally:
            connection.close()

    def list(self):
        return sorted(
            self.backup_dir.glob("inktime-backup-*.zip"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    def validate(self, archive: Path) -> dict:
        """檢查格式、固定成員、大小與每個檔案的 SHA-256。"""

        try:
            with zipfile.ZipFile(archive) as bundle:
                members = bundle.infolist()
                names = {member.filename for member in members}
                if len(members) != len(names):
                    raise ValueError("BACKUP-002 備份含重複檔案名稱")
                if "manifest.json" not in names:
                    raise ValueError("BACKUP-002 備份缺少 manifest.json")
                info = bundle.getinfo("manifest.json")
                if info.file_size > 1024 * 1024:
                    raise ValueError("BACKUP-002 manifest 大小異常")
                manifest = json.loads(bundle.read("manifest.json"))
                version = int(
                    manifest.get("backup_format_version", manifest.get("schema_version", 0))
                )
                expected = (
                    {"inktime.sqlite3", "manifest.json"}
                    if version == 1
                    else {"inktime.sqlite3", "settings.json", "manifest.json"}
                )
                if version not in {1, BACKUP_FORMAT_VERSION} or names != expected:
                    raise ValueError("BACKUP-002 備份內容或格式版本不受支援")
                if bundle.getinfo("inktime.sqlite3").file_size > 1024**4:
                    raise ValueError("BACKUP-002 SQLite 備份大小異常")
                if version == BACKUP_FORMAT_VERSION:
                    files = manifest.get("files") or {}
                    for name in ("inktime.sqlite3", "settings.json"):
                        expected_file = files.get(name) or {}
                        with bundle.open(name) as stream:
                            digest, size = _stream_sha256(stream)
                        if digest != expected_file.get("sha256") or size != int(
                            expected_file.get("size", -1)
                        ):
                            raise ValueError(f"BACKUP-003 {name} 完整性驗證失敗")
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("BACKUP-002 備份格式損壞") from exc
        return manifest

    def _extract_database(self, archive: Path, destination: Path) -> dict:
        manifest = self.validate(archive)
        with zipfile.ZipFile(archive) as bundle, bundle.open("inktime.sqlite3") as source:
            with destination.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        return manifest

    @staticmethod
    def _validate_restore_database(
        path: Path, manifest: dict | None = None, *, require_platform_tables: bool = True
    ) -> dict[str, int]:
        connection = sqlite3.connect(path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise ValueError("RESTORE-002 還原資料庫 integrity_check 失敗")
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            required = {"schema_migrations", "photos", "jobs", "photo_analysis"}
            if require_platform_tables and not required <= tables:
                raise ValueError("RESTORE-002 還原資料庫缺少重要資料表")
            if "migration_history" in tables:
                running = connection.execute(
                    "SELECT 1 FROM migration_history WHERE migration_status='running' LIMIT 1"
                ).fetchone()
                if running:
                    raise ValueError("RESTORE-003 備份含未完成 Migration")
            counts = _database_counts(connection)
            expected = (manifest or {}).get("important_table_counts") or {}
            for table, count in expected.items():
                if table not in counts:
                    raise ValueError(f"RESTORE-004 缺少重要資料表 {table}")
                if int(count) != counts[table]:
                    raise ValueError(f"RESTORE-004 {table} 筆數驗證失敗")
            return counts
        finally:
            connection.close()

    def _snapshot_current(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.backup_dir / f"inktime-pre-restore-{stamp}.sqlite3"
        source = sqlite3.connect(self.database.path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("RESTORE-005 目前資料庫安全副本失敗")
        finally:
            target.close()
            source.close()
        return destination

    def _replace_offline(self, staged: Path, *, manifest: dict | None) -> tuple[Path, dict[str, int]]:
        runtime_lock = self.database.acquire_runtime_lock(exclusive=True, blocking=False)
        safety_copy: Path | None = None
        replaced = False
        try:
            self._validate_restore_database(staged, manifest, require_platform_tables=True)
            staged_database = Database(staged)
            staged_version = staged_database.schema_version()
            latest_version = MIGRATIONS[-1].version
            manifest_version = (manifest or {}).get("database_schema_version")
            if manifest_version is not None and int(manifest_version) != staged_version:
                raise ValueError("RESTORE-006 manifest 與 SQLite Schema Version 不一致")
            if staged_version > latest_version:
                raise ValueError(
                    f"RESTORE-006 備份 Schema Version {staged_version} 高於目前支援版本 {latest_version}"
                )
            if staged_version < latest_version:
                migrate(staged_database)
                self._validate_restore_database(staged)
            with staged_database.session() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            safety_copy = self._snapshot_current()
            for suffix in ("-wal", "-shm"):
                Path(f"{self.database.path}{suffix}").unlink(missing_ok=True)
            os.replace(staged, self.database.path)
            _fsync_directory(self.database.path.parent)
            replaced = True
            counts = self._validate_restore_database(self.database.path, manifest)
            if Database(self.database.path).integrity_check(full=True) != "ok":
                raise ValueError("RESTORE-002 還原後 integrity_check 失敗")
            return safety_copy, counts
        except Exception:
            if replaced and safety_copy is not None and safety_copy.is_file():
                for suffix in ("-wal", "-shm"):
                    Path(f"{self.database.path}{suffix}").unlink(missing_ok=True)
                recovery_handle = tempfile.NamedTemporaryFile(
                    dir=self.database.path.parent,
                    prefix=".inktime-recovery-",
                    suffix=".sqlite3",
                    delete=False,
                )
                recovery = Path(recovery_handle.name)
                recovery_handle.close()
                try:
                    shutil.copy2(safety_copy, recovery)
                    os.replace(recovery, self.database.path)
                    _fsync_directory(self.database.path.parent)
                finally:
                    recovery.unlink(missing_ok=True)
            raise
        finally:
            runtime_lock.close()

    def restore(self, archive: Path) -> dict:
        """離線原子還原；任一步失敗均保持或自動回復原資料庫。"""

        handle = tempfile.NamedTemporaryFile(
            dir=self.database.path.parent,
            prefix=".inktime-restore-",
            suffix=".sqlite3",
            delete=False,
        )
        staged = Path(handle.name)
        handle.close()
        try:
            manifest = self._extract_database(archive, staged)
            safety_copy, counts = self._replace_offline(staged, manifest=manifest)
            return {
                "status": "restored",
                "safety_copy": str(safety_copy),
                "schema_version": Database(self.database.path).schema_version(),
                "important_table_counts": counts,
            }
        finally:
            staged.unlink(missing_ok=True)
            _cleanup_database_sidecars(staged)

    def restore_sqlite_snapshot(self, snapshot: Path) -> dict:
        """供 pre-migration 原始 SQLite 備份使用的相同離線回滾路徑。"""

        handle = tempfile.NamedTemporaryFile(
            dir=self.database.path.parent,
            prefix=".inktime-rollback-",
            suffix=".sqlite3",
            delete=False,
        )
        staged = Path(handle.name)
        handle.close()
        try:
            shutil.copy2(snapshot, staged)
            safety_copy, counts = self._replace_offline(staged, manifest=None)
            return {
                "status": "restored",
                "safety_copy": str(safety_copy),
                "schema_version": Database(self.database.path).schema_version(),
                "important_table_counts": counts,
            }
        finally:
            staged.unlink(missing_ok=True)
            _cleanup_database_sidecars(staged)

    def enforce_retention(self, keep: int) -> int:
        removed = 0
        for path in self.list()[max(1, keep) :]:
            path.unlink()
            removed += 1
        return removed
