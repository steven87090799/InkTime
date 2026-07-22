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
    Migration(
        5,
        "加入評分規則版本與綜合排序分",
        (
            """
            CREATE TABLE IF NOT EXISTS scoring_rule_versions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rules TEXT NOT NULL,
                memory_weight REAL NOT NULL CHECK(memory_weight BETWEEN 0 AND 100),
                beauty_weight REAL NOT NULL CHECK(beauty_weight BETWEEN 0 AND 100),
                technical_weight REAL NOT NULL CHECK(technical_weight BETWEEN 0 AND 100),
                emotion_weight REAL NOT NULL CHECK(emotion_weight BETWEEN 0 AND 100),
                favorite_bonus REAL NOT NULL CHECK(favorite_bonus BETWEEN 0 AND 100),
                is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
                created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                CHECK(abs(memory_weight + beauty_weight + technical_weight + emotion_weight - 100.0) < 0.001)
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_scoring_rule_active ON scoring_rule_versions(is_active) WHERE is_active=1",
            "CREATE INDEX IF NOT EXISTS idx_scoring_rule_created ON scoring_rule_versions(created_at DESC)",
            "ALTER TABLE photo_analysis ADD COLUMN ranking_score REAL CHECK(ranking_score IS NULL OR ranking_score BETWEEN 0 AND 100)",
            "ALTER TABLE photo_analysis ADD COLUMN scoring_version_id TEXT REFERENCES scoring_rule_versions(id) ON DELETE SET NULL",
            "CREATE INDEX IF NOT EXISTS idx_photo_analysis_ranking ON photo_analysis(ranking_score DESC)",
        ),
    ),
    Migration(
        6,
        "加入 ESP32 遠端設定與低頻狀態回報",
        (
            "ALTER TABLE devices ADD COLUMN free_heap_bytes INTEGER",
            "ALTER TABLE devices ADD COLUMN free_psram_bytes INTEGER",
            "ALTER TABLE devices ADD COLUMN last_error_code TEXT",
            "ALTER TABLE devices ADD COLUMN last_error_message TEXT",
            "ALTER TABLE devices ADD COLUMN last_status_at TEXT",
            "ALTER TABLE devices ADD COLUMN wake_reason TEXT",
            "UPDATE devices SET schedule='08:00' WHERE schedule='daily' OR schedule=''",
            """
            CREATE TABLE IF NOT EXISTS device_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                level TEXT NOT NULL CHECK(level IN ('info','warning','error')),
                event TEXT NOT NULL,
                error_code TEXT,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_device_events_device_time ON device_events(device_id,created_at DESC)",
        ),
    ),
    Migration(
        7,
        "加入全彩 Profile、裝置設定 ACK 與離線通知",
        (
            "ALTER TABLE devices ADD COLUMN panel_profile TEXT NOT NULL DEFAULT 'safe_4c'",
            "ALTER TABLE devices ADD COLUMN config_version INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE devices ADD COLUMN acked_config_version INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE devices ADD COLUMN config_ack_at TEXT",
            "ALTER TABLE devices ADD COLUMN offline_alert_active INTEGER NOT NULL DEFAULT 0 CHECK(offline_alert_active IN (0,1))",
            "ALTER TABLE devices ADD COLUMN last_offline_alert_at TEXT",
            "ALTER TABLE devices ADD COLUMN last_recovery_alert_at TEXT",
            "ALTER TABLE releases ADD COLUMN render_profile TEXT NOT NULL DEFAULT 'safe_4c'",
            "CREATE INDEX IF NOT EXISTS idx_releases_profile_created ON releases(render_profile,created_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS device_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT REFERENCES devices(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('offline','offline_reminder','recovery','test')),
                level TEXT NOT NULL CHECK(level IN ('info','warning','error')),
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                webhook_status TEXT NOT NULL DEFAULT 'disabled'
                    CHECK(webhook_status IN ('disabled','pending','retrying','delivered','failed')),
                webhook_attempts INTEGER NOT NULL DEFAULT 0,
                webhook_next_attempt_at TEXT,
                webhook_delivered_at TEXT,
                webhook_last_error TEXT,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_device_notifications_created ON device_notifications(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_device_notifications_delivery ON device_notifications(webhook_status,webhook_next_attempt_at,id)",
            "UPDATE feature_flags SET enabled=1,description='裝置離線／恢復站內通知與可選 Webhook 已啟用' WHERE key='notifications'",
        ),
    ),
    Migration(
        8,
        "加入裝置能源曲線與續航估算設定",
        (
            "ALTER TABLE devices ADD COLUMN battery_capacity_mah REAL CHECK(battery_capacity_mah IS NULL OR battery_capacity_mah BETWEEN 10 AND 100000)",
            "ALTER TABLE devices ADD COLUMN standby_current_ma REAL CHECK(standby_current_ma IS NULL OR standby_current_ma BETWEEN 0.001 AND 10000)",
            "ALTER TABLE devices ADD COLUMN active_current_ma REAL CHECK(active_current_ma IS NULL OR active_current_ma BETWEEN 0.001 AND 10000)",
            "ALTER TABLE devices ADD COLUMN refreshes_per_day REAL NOT NULL DEFAULT 1 CHECK(refreshes_per_day BETWEEN 0.01 AND 96)",
            "ALTER TABLE devices ADD COLUMN battery_reserve_percent REAL NOT NULL DEFAULT 10 CHECK(battery_reserve_percent BETWEEN 0 AND 50)",
            "ALTER TABLE devices ADD COLUMN energy_profile_updated_at TEXT",
            """
            CREATE TABLE IF NOT EXISTS device_power_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                battery_voltage REAL CHECK(battery_voltage IS NULL OR battery_voltage BETWEEN 0 AND 10),
                battery_percent REAL CHECK(battery_percent IS NULL OR battery_percent BETWEEN 0 AND 100),
                battery_percent_estimated INTEGER CHECK(battery_percent_estimated IS NULL OR battery_percent_estimated IN (0,1)),
                usb_power INTEGER CHECK(usb_power IS NULL OR usb_power IN (0,1)),
                refresh_duration_ms INTEGER CHECK(refresh_duration_ms IS NULL OR refresh_duration_ms BETWEEN 0 AND 600000),
                wake_duration_ms INTEGER CHECK(wake_duration_ms IS NULL OR wake_duration_ms BETWEEN 0 AND 86400000),
                display_updated INTEGER NOT NULL DEFAULT 0 CHECK(display_updated IN (0,1)),
                temperature_c REAL CHECK(temperature_c IS NULL OR temperature_c BETWEEN -100 AND 150),
                wifi_rssi INTEGER CHECK(wifi_rssi IS NULL OR wifi_rssi BETWEEN -127 AND 0),
                wake_reason TEXT,
                recorded_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_device_power_samples_device_time ON device_power_samples(device_id,recorded_at DESC,id DESC)",
            "INSERT OR IGNORE INTO feature_flags(key,enabled,description,updated_at) VALUES ('device_energy',1,'裝置電池曲線、刷新耗時與續航估算儀表板已啟用',datetime('now'))",
        ),
    ),
    Migration(
        9,
        "啟用內建繁體中文手寫字型",
        (
            "UPDATE settings SET value_json='\"builtin:iansui\"',updated_at=datetime('now') "
            "WHERE key='render.font_path' AND value_json='\"\"'",
        ),
    ),
    Migration(
        10,
        "加入智慧裁切、E6 適合度與室內濕度",
        (
            "ALTER TABLE photos ADD COLUMN crop_focus_x REAL CHECK(crop_focus_x IS NULL OR crop_focus_x BETWEEN 0 AND 1)",
            "ALTER TABLE photos ADD COLUMN crop_focus_y REAL CHECK(crop_focus_y IS NULL OR crop_focus_y BETWEEN 0 AND 1)",
            "ALTER TABLE photos ADD COLUMN crop_subject_left REAL CHECK(crop_subject_left IS NULL OR crop_subject_left BETWEEN 0 AND 1)",
            "ALTER TABLE photos ADD COLUMN crop_subject_top REAL CHECK(crop_subject_top IS NULL OR crop_subject_top BETWEEN 0 AND 1)",
            "ALTER TABLE photos ADD COLUMN crop_subject_right REAL CHECK(crop_subject_right IS NULL OR crop_subject_right BETWEEN 0 AND 1)",
            "ALTER TABLE photos ADD COLUMN crop_subject_bottom REAL CHECK(crop_subject_bottom IS NULL OR crop_subject_bottom BETWEEN 0 AND 1)",
            "ALTER TABLE photos ADD COLUMN crop_method TEXT",
            "ALTER TABLE photos ADD COLUMN crop_face_count INTEGER NOT NULL DEFAULT 0 CHECK(crop_face_count >= 0)",
            "ALTER TABLE photos ADD COLUMN crop_manual_x REAL CHECK(crop_manual_x IS NULL OR crop_manual_x BETWEEN 0 AND 1)",
            "ALTER TABLE photos ADD COLUMN crop_manual_y REAL CHECK(crop_manual_y IS NULL OR crop_manual_y BETWEEN 0 AND 1)",
            "ALTER TABLE photos ADD COLUMN e6_score REAL CHECK(e6_score IS NULL OR e6_score BETWEEN 0 AND 100)",
            "ALTER TABLE photos ADD COLUMN e6_contrast_score REAL CHECK(e6_contrast_score IS NULL OR e6_contrast_score BETWEEN 0 AND 100)",
            "ALTER TABLE photos ADD COLUMN e6_subject_score REAL CHECK(e6_subject_score IS NULL OR e6_subject_score BETWEEN 0 AND 100)",
            "ALTER TABLE photos ADD COLUMN e6_skin_score REAL CHECK(e6_skin_score IS NULL OR e6_skin_score BETWEEN 0 AND 100)",
            "ALTER TABLE photos ADD COLUMN e6_text_score REAL CHECK(e6_text_score IS NULL OR e6_text_score BETWEEN 0 AND 100)",
            "ALTER TABLE photos ADD COLUMN e6_skin_pixels INTEGER NOT NULL DEFAULT 0 CHECK(e6_skin_pixels >= 0)",
            "ALTER TABLE device_power_samples ADD COLUMN humidity_percent REAL CHECK(humidity_percent IS NULL OR humidity_percent BETWEEN 0 AND 100)",
            "CREATE INDEX IF NOT EXISTS idx_photos_history_day ON photos(substr(captured_at,6,5),captured_at)",
            "CREATE INDEX IF NOT EXISTS idx_photos_e6_score ON photos(e6_score DESC)",
            "INSERT OR IGNORE INTO feature_flags(key,enabled,description,updated_at) VALUES ('smart_composition',1,'智慧裁切、六色適合度與相框版型已啟用',datetime('now'))",
        ),
    ),
    Migration(
        11,
        "加入安全掃描生命週期、錯誤與 Migration 歷史",
        (
            """
            CREATE TABLE IF NOT EXISTS migration_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version INTEGER NOT NULL,
                migration_name TEXT NOT NULL,
                migration_started_at TEXT NOT NULL,
                migration_completed_at TEXT,
                migration_status TEXT NOT NULL
                    CHECK(migration_status IN ('running','completed','rolled_back')),
                backup_path TEXT,
                error_message TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_migration_history_status ON migration_history(migration_status,id DESC)",
            """
            INSERT INTO migration_history(
                schema_version,migration_name,migration_started_at,migration_completed_at,
                migration_status
            )
            SELECT sm.version,sm.name,sm.applied_at,sm.applied_at,'completed'
            FROM schema_migrations sm
            WHERE sm.version < 11 AND NOT EXISTS (
                SELECT 1 FROM migration_history mh WHERE mh.schema_version=sm.version
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS scan_runs (
                id TEXT PRIMARY KEY,
                library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
                mode TEXT NOT NULL
                    CHECK(mode IN ('incremental','full','metadata-only','local-features-only','manual')),
                trigger_source TEXT NOT NULL DEFAULT 'manual'
                    CHECK(trigger_source IN ('manual','api','scheduler','virtual-display','test')),
                status TEXT NOT NULL
                    CHECK(status IN ('running','completed','completed_with_warnings','cancelled','failed')),
                root_path TEXT NOT NULL,
                root_accessible INTEGER NOT NULL DEFAULT 0 CHECK(root_accessible IN (0,1)),
                root_readable INTEGER NOT NULL DEFAULT 0 CHECK(root_readable IN (0,1)),
                full_census INTEGER NOT NULL DEFAULT 0 CHECK(full_census IN (0,1)),
                cancelled INTEGER NOT NULL DEFAULT 0 CHECK(cancelled IN (0,1)),
                major_io_errors INTEGER NOT NULL DEFAULT 0,
                checked_count INTEGER NOT NULL DEFAULT 0,
                processed_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0,
                changed_count INTEGER NOT NULL DEFAULT 0,
                moved_count INTEGER NOT NULL DEFAULT 0,
                restored_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                excluded_video_count INTEGER NOT NULL DEFAULT 0,
                previous_active_count INTEGER NOT NULL DEFAULT 0,
                candidate_missing_count INTEGER NOT NULL DEFAULT 0,
                missing_marked_count INTEGER NOT NULL DEFAULT 0,
                missing_threshold_ratio REAL NOT NULL DEFAULT 0.10
                    CHECK(missing_threshold_ratio BETWEEN 0 AND 1),
                reconciliation_status TEXT NOT NULL DEFAULT 'not_run'
                    CHECK(reconciliation_status IN ('not_run','applied','skipped','confirmation_required','confirmed')),
                warning_code TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_scan_runs_library_started ON scan_runs(library_id,started_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS scan_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
                stage TEXT NOT NULL,
                error_code TEXT NOT NULL,
                exception_type TEXT NOT NULL,
                retryable INTEGER NOT NULL CHECK(retryable IN (0,1)),
                masked_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_scan_errors_scan ON scan_errors(scan_id,id)",
            """
            CREATE TABLE IF NOT EXISTS scan_missing_candidates (
                scan_id TEXT NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY(scan_id,photo_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_scan_missing_candidates_photo ON scan_missing_candidates(photo_id,scan_id)",
            "ALTER TABLE photos ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active' CHECK(lifecycle_status IN ('active','missing','excluded','archived','deleted'))",
            "ALTER TABLE photos ADD COLUMN missing_since TEXT",
            "ALTER TABLE photos ADD COLUMN missing_reason TEXT",
            "ALTER TABLE photos ADD COLUMN last_seen_scan_id TEXT REFERENCES scan_runs(id) ON DELETE SET NULL",
            "ALTER TABLE photos ADD COLUMN metadata_status TEXT NOT NULL DEFAULT 'pending' CHECK(metadata_status IN ('pending','complete','failed'))",
            "ALTER TABLE photos ADD COLUMN local_features_status TEXT NOT NULL DEFAULT 'pending' CHECK(local_features_status IN ('pending','complete','failed'))",
            "UPDATE photos SET metadata_status=CASE WHEN sha256 IS NOT NULL THEN 'complete' ELSE 'pending' END, local_features_status=CASE WHEN sha256 IS NOT NULL THEN 'complete' ELSE 'pending' END",
            "CREATE INDEX IF NOT EXISTS idx_photos_lifecycle_seen ON photos(library_id,lifecycle_status,last_seen_scan_id,id)",
            "CREATE INDEX IF NOT EXISTS idx_photos_scan_incomplete ON photos(library_id,metadata_status,local_features_status,id)",
        ),
    ),
    Migration(
        12,
        "加入低資源排程、優先佇列與快取保留",
        (
            "ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 4 CHECK(priority BETWEEN 1 AND 6)",
            "ALTER TABLE jobs ADD COLUMN dedupe_key TEXT",
            "ALTER TABLE job_items ADD COLUMN dead_lettered_at TEXT",
            "CREATE INDEX IF NOT EXISTS idx_jobs_runnable_priority ON jobs(status,priority,created_at,id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_dedupe ON jobs(dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('pending','preparing','running','pausing','retrying')",
            "CREATE INDEX IF NOT EXISTS idx_job_items_dead_letter ON job_items(dead_lettered_at) WHERE dead_lettered_at IS NOT NULL",
            """
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                cron TEXT NOT NULL,
                weekdays_json TEXT NOT NULL DEFAULT '[]',
                start_time TEXT NOT NULL DEFAULT '00:00',
                window_start TEXT,
                window_end TEXT,
                timeout_seconds INTEGER NOT NULL DEFAULT 3600 CHECK(timeout_seconds BETWEEN 30 AND 86400),
                retry_count INTEGER NOT NULL DEFAULT 2 CHECK(retry_count BETWEEN 0 AND 10),
                retry_interval_seconds INTEGER NOT NULL DEFAULT 900 CHECK(retry_interval_seconds BETWEEN 30 AND 86400),
                last_success TEXT,
                last_failure TEXT,
                next_run TEXT,
                error_status TEXT,
                config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due ON scheduled_tasks(enabled,next_run,key)",
        ),
    ),
    Migration(
        13,
        "加入本地品質排除、AI 快取與旅行排序",
        (
            "ALTER TABLE photos ADD COLUMN eligible INTEGER NOT NULL DEFAULT 1 CHECK(eligible IN (0,1))",
            "ALTER TABLE photos ADD COLUMN exclusion_status TEXT NOT NULL DEFAULT 'eligible' CHECK(exclusion_status IN ('eligible','auto_excluded','manually_excluded','manually_restored','pending_review'))",
            "ALTER TABLE photos ADD COLUMN reject_reason TEXT",
            "ALTER TABLE photos ADD COLUMN reject_rule TEXT",
            "ALTER TABLE photos ADD COLUMN reject_rule_version TEXT",
            "ALTER TABLE photos ADD COLUMN reject_details_json TEXT",
            "ALTER TABLE photos ADD COLUMN rejected_at TEXT",
            "ALTER TABLE photos ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0 CHECK(manual_override IN (0,1))",
            "ALTER TABLE photos ADD COLUMN local_candidate_score REAL CHECK(local_candidate_score IS NULL OR local_candidate_score BETWEEN 0 AND 100)",
            "ALTER TABLE photos ADD COLUMN feature_version TEXT NOT NULL DEFAULT 'local-quality-v3'",
            "ALTER TABLE photos ADD COLUMN orientation INTEGER",
            "ALTER TABLE photos ADD COLUMN camera_make TEXT",
            "ALTER TABLE photos ADD COLUMN camera_model TEXT",
            "ALTER TABLE photos ADD COLUMN lens_model TEXT",
            "ALTER TABLE photo_analysis ADD COLUMN schema_kind TEXT NOT NULL DEFAULT 'basic'",
            "ALTER TABLE photo_analysis ADD COLUMN semantic_json TEXT",
            "ALTER TABLE photo_analysis ADD COLUMN local_score REAL",
            "ALTER TABLE photo_analysis ADD COLUMN semantic_score REAL",
            "ALTER TABLE photo_analysis ADD COLUMN base_ranking_score REAL",
            "ALTER TABLE photo_analysis ADD COLUMN final_ranking_score REAL",
            "ALTER TABLE photo_analysis ADD COLUMN ranking_rule_version TEXT NOT NULL DEFAULT 'ranking-v2'",
            "ALTER TABLE photo_analysis ADD COLUMN travel_bonus REAL NOT NULL DEFAULT 0",
            "ALTER TABLE photo_analysis ADD COLUMN location_rule_version TEXT",
            "CREATE INDEX IF NOT EXISTS idx_photos_exclusion_status ON photos(exclusion_status,rejected_at DESC,id)",
            "CREATE INDEX IF NOT EXISTS idx_photos_eligible_candidate ON photos(eligible,local_candidate_score DESC,id)",
            """
            CREATE TABLE IF NOT EXISTS ai_analysis_cache (
                content_sha256 TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_name TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                schema_kind TEXT NOT NULL CHECK(schema_kind IN ('basic','full')),
                result_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost REAL NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                PRIMARY KEY(content_sha256,provider,model_name,prompt_version,schema_version,schema_kind)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_ai_cache_created ON ai_analysis_cache(created_at DESC)",
            "INSERT OR IGNORE INTO feature_flags(key,enabled,description,updated_at) VALUES ('photo_quality_ai',1,'本地品質、排除管理、結構化 AI 快取與旅行排序已啟用',datetime('now'))",
        ),
    ),
    Migration(
        14,
        "加入歷史今日顯示紀錄與 Prompt 版本",
        (
            "ALTER TABLE photo_analysis ADD COLUMN prompt_version TEXT NOT NULL DEFAULT 'photo-quality-v3'",
            "CREATE INDEX IF NOT EXISTS idx_photos_history_selection ON photos(eligible,lifecycle_status,captured_at,id)",
            """
            CREATE TABLE IF NOT EXISTS display_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE RESTRICT,
                history_date TEXT NOT NULL,
                selection_method TEXT NOT NULL,
                release_id TEXT,
                displayed_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_display_history_photo ON display_history(photo_id,displayed_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_display_history_date ON display_history(history_date,displayed_at DESC)",
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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{database.path.stem}-pre-migration-{stamp}.sqlite3"
    source = sqlite3.connect(database.path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError("升級前備份完整性檢查失敗")
    finally:
        target.close()
        source.close()
    return destination


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _applied_versions(database: Database) -> set[int]:
    with database.session() as connection:
        if not _table_exists(connection, "schema_migrations"):
            return set()
        return {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}


def _assert_no_unfinished_migration(database: Database) -> None:
    with database.session() as connection:
        if not _table_exists(connection, "migration_history"):
            return
        unfinished = connection.execute(
            """
            SELECT schema_version,migration_name,backup_path
            FROM migration_history
            WHERE migration_status='running'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    if unfinished is None:
        return
    recovery = str(unfinished["backup_path"] or "最近一次 pre-migration SQLite 備份")
    raise MigrationError(
        "MIGRATION-002 偵測到未完成 Migration "
        f"{unfinished['schema_version']}（{unfinished['migration_name']}）；平台已停止啟動，"
        f"不得繼續寫入。請停止所有 InkTime 程序後由 {recovery} 還原。"
    )


def _start_history(
    database: Database,
    migration: Migration,
    backup_path: Path | None,
    *,
    started_at: str,
) -> int | None:
    with database.session() as connection:
        if not _table_exists(connection, "migration_history"):
            return None
        cursor = connection.execute(
            """
            INSERT INTO migration_history(
                schema_version,migration_name,migration_started_at,migration_status,backup_path
            ) VALUES (?,?,?,'running',?)
            """,
            (
                migration.version,
                migration.name,
                started_at,
                str(backup_path) if backup_path else None,
            ),
        )
        return int(cursor.lastrowid) if cursor.lastrowid is not None else None


def _finish_history(
    database: Database,
    migration: Migration,
    history_id: int | None,
    *,
    started_at: str,
    status: str,
    backup_path: Path | None,
    error: str | None = None,
) -> None:
    with database.session() as connection:
        if not _table_exists(connection, "migration_history"):
            return
        if history_id is None:
            connection.execute(
                """
                INSERT INTO migration_history(
                    schema_version,migration_name,migration_started_at,migration_completed_at,
                    migration_status,backup_path,error_message
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    migration.version,
                    migration.name,
                    started_at,
                    _utc_now(),
                    status,
                    str(backup_path) if backup_path else None,
                    error[:1000] if error else None,
                ),
            )
            return
        connection.execute(
            """
            UPDATE migration_history
            SET migration_completed_at=?,migration_status=?,error_message=?
            WHERE id=?
            """,
            (_utc_now(), status, error[:1000] if error else None, history_id),
        )


def migrate(database: Database, backup_dir: Path | None = None) -> list[int]:
    """依版本安全升級；schema、版本列與完整性檢查位於同一交易。"""
    had_database = database.path.exists() and database.path.stat().st_size > 0
    database.path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{database.path}.migration.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _assert_no_unfinished_migration(database)
        applied_versions = _applied_versions(database)
        known_versions = {migration.version for migration in MIGRATIONS}
        unknown_versions = applied_versions - known_versions
        if unknown_versions:
            newest = max(unknown_versions)
            raise MigrationError(
                f"MIGRATION-003 資料庫 Schema Version {newest} 高於本程式可支援版本；停止啟動以避免降級寫入"
            )
        has_pending_migrations = any(
            migration.version not in applied_versions for migration in MIGRATIONS
        )
        # 只有真的要升級既有資料庫才建立備份；三個容器每次重啟不再各複製一次。
        backup_path = None
        if backup_dir is not None and had_database and has_pending_migrations:
            backup_path = backup_database(database, backup_dir)

        applied: list[int] = []
        for migration in MIGRATIONS:
            if migration.version in applied_versions:
                continue
            started_at = _utc_now()
            history_id = _start_history(
                database, migration, backup_path, started_at=started_at
            )
            history_completed_in_transaction = False
            try:
                with database.transaction() as connection:
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, _utc_now()),
                    )
                    if history_id is None and _table_exists(
                        connection, "migration_history"
                    ):
                        connection.execute(
                            """
                            INSERT INTO migration_history(
                                schema_version,migration_name,migration_started_at,
                                migration_completed_at,migration_status,backup_path
                            ) VALUES (?,?,?,?,'completed',?)
                            """,
                            (
                                migration.version,
                                migration.name,
                                started_at,
                                _utc_now(),
                                str(backup_path) if backup_path else None,
                            ),
                        )
                        history_completed_in_transaction = True
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()
                    if integrity is None or str(integrity[0]) != "ok":
                        raise MigrationError(
                            f"Migration {migration.version} 完整性檢查失敗：{integrity[0] if integrity else 'unknown'}"
                        )
            except Exception as exc:
                _finish_history(
                    database,
                    migration,
                    history_id,
                    started_at=started_at,
                    status="rolled_back",
                    backup_path=backup_path,
                    error=str(exc),
                )
                raise MigrationError(
                    f"Migration {migration.version}（{migration.name}）失敗；Schema 已完整 Rollback"
                ) from exc
            if not history_completed_in_transaction:
                try:
                    _finish_history(
                        database,
                        migration,
                        history_id,
                        started_at=started_at,
                        status="completed",
                        backup_path=backup_path,
                    )
                except Exception as exc:
                    raise MigrationError(
                        "MIGRATION-004 Schema 已提交但 Migration 歷史無法完成；"
                        "平台必須停止，請由升級前備份回復，不得繼續寫入"
                    ) from exc
            applied.append(migration.version)
            applied_versions.add(migration.version)
        return applied
