from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from pathlib import Path
import sqlite3

from .connection import Database


class MigrationError(RuntimeError):
    """資料庫升級未完成；呼叫端必須停止啟動。"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        1,
        "建立平台核心資料表",
        (
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS libraries (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                root_path TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS photos (
                id TEXT PRIMARY KEY,
                library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
                relative_path TEXT NOT NULL,
                file_size INTEGER,
                modified_time REAL,
                sha256 TEXT,
                perceptual_hash TEXT,
                difference_hash TEXT,
                width INTEGER,
                height INTEGER,
                format TEXT,
                status TEXT NOT NULL DEFAULT 'discovered',
                favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0, 1)),
                duplicate_group_id TEXT,
                analysis_source TEXT NOT NULL DEFAULT 'direct',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(library_id, relative_path)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_photos_status_id ON photos(status, id)",
            "CREATE INDEX IF NOT EXISTS idx_photos_sha256 ON photos(sha256)",
            "CREATE INDEX IF NOT EXISTS idx_photos_phash ON photos(perceptual_hash)",
            "CREATE INDEX IF NOT EXISTS idx_photos_modified ON photos(modified_time)",
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                strategy TEXT NOT NULL,
                settings_json TEXT NOT NULL,
                budget_limit REAL,
                spent REAL NOT NULL DEFAULT 0,
                total_items INTEGER NOT NULL DEFAULT 0,
                completed_items INTEGER NOT NULL DEFAULT 0,
                failed_items INTEGER NOT NULL DEFAULT 0,
                created_by TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                heartbeat_at TEXT,
                pause_requested_at TEXT,
                cancel_requested_at TEXT,
                CHECK(status IN ('pending','preparing','running','pausing','paused','retrying','completed','completed_with_errors','failed','cancelled','budget_exceeded'))
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)",
            """
            CREATE TABLE IF NOT EXISTS job_items (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                stage TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                worker_id TEXT,
                started_at TEXT,
                completed_at TEXT,
                error_code TEXT,
                result_json TEXT,
                UNIQUE(job_id, photo_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_job_items_claim ON job_items(job_id, status, available_at, id)",
            """
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                event TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id DESC)",
            """
            CREATE TABLE IF NOT EXISTS job_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
                job_item_id TEXT REFERENCES job_items(id) ON DELETE SET NULL,
                photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
                component TEXT NOT NULL,
                error_code TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_note TEXT,
                UNIQUE(fingerprint, resolved_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_job_errors_unresolved ON job_errors(resolved_at, last_seen_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
                request_type TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost REAL NOT NULL DEFAULT 0,
                actual_cost REAL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                latency_ms INTEGER,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_api_usage_time ON api_usage(started_at)",
            "CREATE INDEX IF NOT EXISTS idx_api_usage_job ON api_usage(job_id, photo_id)",
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('administrator','viewer')),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                password_changed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                succeeded INTEGER NOT NULL,
                attempted_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts(ip_address, attempted_at)",
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                value_json TEXT NOT NULL,
                value_type TEXT NOT NULL,
                requires_restart INTEGER NOT NULL DEFAULT 0,
                updated_by TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS setting_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT,
                old_value_summary TEXT,
                new_value_summary TEXT,
                source_ip TEXT,
                requires_restart INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS secrets (
                key TEXT PRIMARY KEY,
                encrypted_value BLOB NOT NULL,
                updated_by TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                firmware_version TEXT,
                timezone TEXT NOT NULL DEFAULT 'Asia/Taipei',
                schedule TEXT NOT NULL DEFAULT '05:00',
                rotation INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT,
                last_ip TEXT,
                last_download_at TEXT,
                last_release_id TEXT,
                download_success_count INTEGER NOT NULL DEFAULT 0,
                download_failure_count INTEGER NOT NULL DEFAULT 0,
                wifi_rssi INTEGER,
                battery_percent REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS releases (
                id TEXT PRIMARY KEY,
                display_type TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                pixel_format TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                published_at TEXT,
                created_by TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS feature_flags (
                key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                description TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        2,
        "加入工作租約與分析結果",
        (
            "ALTER TABLE job_items ADD COLUMN lease_until TEXT",
            "ALTER TABLE job_items ADD COLUMN estimated_cost REAL NOT NULL DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_job_items_lease ON job_items(status, lease_until)",
            """
            CREATE TABLE IF NOT EXISTS photo_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                schema_version INTEGER NOT NULL,
                stage TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                caption TEXT,
                types_json TEXT NOT NULL DEFAULT '[]',
                memory_score REAL,
                beauty_score REAL,
                technical_quality_score REAL,
                emotion_score REAL,
                side_caption TEXT,
                should_keep INTEGER,
                sensitive INTEGER,
                reason TEXT,
                raw_json TEXT NOT NULL,
                analysis_source TEXT NOT NULL DEFAULT 'direct',
                created_at TEXT NOT NULL,
                CHECK(memory_score IS NULL OR memory_score BETWEEN 0 AND 100),
                CHECK(beauty_score IS NULL OR beauty_score BETWEEN 0 AND 100),
                CHECK(technical_quality_score IS NULL OR technical_quality_score BETWEEN 0 AND 100),
                CHECK(emotion_score IS NULL OR emotion_score BETWEEN 0 AND 100)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_photo_analysis_photo ON photo_analysis(photo_id, created_at DESC)",
        ),
    ),
    Migration(
        3,
        "加入本地影像特徵與 Provider 設定",
        (
            "ALTER TABLE photos ADD COLUMN exif_json TEXT",
            "ALTER TABLE photos ADD COLUMN captured_at TEXT",
            "ALTER TABLE photos ADD COLUMN gps_lat REAL",
            "ALTER TABLE photos ADD COLUMN gps_lon REAL",
            "ALTER TABLE photos ADD COLUMN brightness REAL",
            "ALTER TABLE photos ADD COLUMN contrast REAL",
            "ALTER TABLE photos ADD COLUMN blur_score REAL",
            "ALTER TABLE photos ADD COLUMN overexposed_ratio REAL",
            "ALTER TABLE photos ADD COLUMN underexposed_ratio REAL",
            "ALTER TABLE photos ADD COLUMN screenshot_likelihood REAL",
            """
            CREATE TABLE IF NOT EXISTS providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key_secret TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 100,
                supports_vision INTEGER NOT NULL DEFAULT 1,
                supports_batch INTEGER NOT NULL DEFAULT 0,
                supports_json_schema INTEGER NOT NULL DEFAULT 1,
                rate_limit_rpm INTEGER,
                token_limit_tpm INTEGER,
                max_concurrency INTEGER NOT NULL DEFAULT 2,
                timeout_seconds INTEGER NOT NULL DEFAULT 120,
                cooldown_seconds INTEGER NOT NULL DEFAULT 300,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS model_pricing (
                provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                input_per_million REAL NOT NULL DEFAULT 0,
                cached_input_per_million REAL NOT NULL DEFAULT 0,
                output_per_million REAL NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(provider_id, model)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_photos_captured ON photos(captured_at)",
            "CREATE INDEX IF NOT EXISTS idx_photos_duplicate ON photos(duplicate_group_id)",
        ),
    ),
    Migration(
        4,
        "加入照片人工修正歷史與功能旗標",
        (
            """
            CREATE TABLE IF NOT EXISTS photo_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                event TEXT NOT NULL,
                changes_json TEXT NOT NULL,
                changed_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_photo_events_photo ON photo_events(photo_id,created_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS feature_flags (
                key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                description TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "INSERT OR IGNORE INTO feature_flags(key,enabled,description,updated_at) VALUES ('face_groups',0,'人臉群組（尚未啟用）',datetime('now'))",
            "INSERT OR IGNORE INTO feature_flags(key,enabled,description,updated_at) VALUES ('notifications',0,'Webhook、Email 與即時通訊通知（尚未啟用）',datetime('now'))",
            "INSERT OR IGNORE INTO feature_flags(key,enabled,description,updated_at) VALUES ('remote_workers',0,'遠端或 GPU Worker（尚未啟用）',datetime('now'))",
            "INSERT OR IGNORE INTO feature_flags(key,enabled,description,updated_at) VALUES ('object_storage',0,'S3 相容物件儲存（尚未啟用）',datetime('now'))",
        ),
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def backup_database(database: Database, backup_dir: Path) -> Path | None:
    """升級前建立一致的 SQLite 備份；全新安裝不產生空備份。"""
    if not database.path.exists() or database.path.stat().st_size == 0:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"{database.path.stem}-pre-migration-{stamp}.sqlite3"
    source = sqlite3.connect(database.path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MigrationError("升級前備份完整性檢查失敗")
    finally:
        target.close()
        source.close()
    return destination


def migrate(database: Database, backup_dir: Path | None = None) -> list[int]:
    """依版本套用 Migration；任何失敗都會回滾當次版本並停止。"""
    database.path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{database.path}.migration.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if backup_dir is not None:
            backup_database(database, backup_dir)

        applied: list[int] = []
        with database.session() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            for migration in MIGRATIONS:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    already_applied = connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version=?", (migration.version,)
                    ).fetchone()
                    if already_applied:
                        connection.execute("COMMIT")
                        continue
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, _utc_now()),
                    )
                    connection.execute("COMMIT")
                    applied.append(migration.version)
                except Exception as exc:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise MigrationError(f"Migration {migration.version}（{migration.name}）失敗") from exc
        return applied
