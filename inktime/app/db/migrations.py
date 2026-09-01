from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import sqlite3
import time
from urllib.parse import urlparse

from inktime.app.core.locks import fcntl
from inktime.app.core.logging import log_event

from .connection import Database


LOGGER = logging.getLogger("migration")


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
    Migration(
        15,
        "加入正式發布恢復、AI 單飛租約與副作用冪等鍵",
        (
            "ALTER TABLE releases ADD COLUMN failure_reason TEXT",
            "ALTER TABLE releases ADD COLUMN verified_at TEXT",
            "ALTER TABLE releases ADD COLUMN reconciliation_status TEXT NOT NULL DEFAULT 'ok'",
            "ALTER TABLE job_items ADD COLUMN idempotency_key TEXT",
            "ALTER TABLE job_items ADD COLUMN completion_state TEXT NOT NULL DEFAULT 'on_time'",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_items_idempotency ON job_items(idempotency_key) WHERE idempotency_key IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_releases_status_created ON releases(status,created_at,id)",
            """
            CREATE TABLE IF NOT EXISTS ai_cache_reservations (
                cache_key TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('reserved','completed','failed')),
                lease_until TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_ai_cache_reservation_lease ON ai_cache_reservations(status,lease_until)",
            """
            CREATE TABLE IF NOT EXISTS device_auth_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_hash TEXT NOT NULL,
                attempted_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_device_auth_failures_ip_time ON device_auth_failures(ip_hash,attempted_at)",
        ),
    ),
    Migration(
        16,
        "加入每台裝置的智慧相框版型與發布指派",
        (
            "ALTER TABLE devices ADD COLUMN frame_orientation TEXT CHECK(frame_orientation IN ('portrait','landscape') OR frame_orientation IS NULL)",
            "ALTER TABLE devices ADD COLUMN layout_mode TEXT CHECK(layout_mode IN ('adaptive_memory','full','postcard','photo_info','photo_pair','calendar','weather_sensor') OR layout_mode IS NULL)",
            "ALTER TABLE devices ADD COLUMN fit_mode TEXT CHECK(fit_mode IN ('contain','cover') OR fit_mode IS NULL)",
            """
            CREATE TABLE IF NOT EXISTS device_render_releases (
                device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE RESTRICT,
                assigned_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_device_render_releases_release ON device_render_releases(release_id)",
        ),
    ),
    Migration(
        17,
        "加入有界系統監控事件",
        (
            "CREATE TABLE IF NOT EXISTS activity_events (id INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT NOT NULL DEFAULT 'activity',source_id TEXT,severity TEXT NOT NULL,component TEXT NOT NULL,event TEXT NOT NULL,message TEXT NOT NULL,job_id TEXT,photo_id TEXT,device_id TEXT,stage TEXT,progress_done INTEGER,progress_total INTEGER,error_code TEXT,trace_id TEXT,details_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_activity_events_id ON activity_events(id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_activity_events_filter ON activity_events(severity,component,id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_activity_events_severity_id ON activity_events(severity,id DESC)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_events_source ON activity_events(source,source_id) WHERE source_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_activity_events_time ON activity_events(created_at DESC,id DESC)",
            "CREATE TABLE IF NOT EXISTS observability_state (key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at TEXT NOT NULL)",
        ),
    ),
    Migration(
        18,
        "加入版本化設定快照與安全回復",
        (
            """
        CREATE TABLE IF NOT EXISTS settings_snapshots (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            actor_id TEXT,
            source_ip TEXT NOT NULL,
            reason TEXT,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            changed_keys_json TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            application_version TEXT NOT NULL,
            rollback_source_snapshot_id TEXT
                REFERENCES settings_snapshots(id) ON DELETE RESTRICT
        )
        """,
            """
        CREATE TABLE IF NOT EXISTS settings_snapshot_items (
            snapshot_id TEXT NOT NULL
                REFERENCES settings_snapshots(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            old_value_json TEXT NOT NULL,
            new_value_json TEXT NOT NULL,
            restored_default INTEGER NOT NULL DEFAULT 0
                CHECK(restored_default IN (0,1)),
            PRIMARY KEY(snapshot_id,key)
        )
        """,
            "CREATE INDEX IF NOT EXISTS idx_settings_snapshots_created ON settings_snapshots(created_at DESC,id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_settings_snapshot_items_key ON settings_snapshot_items(key,snapshot_id)",
            "CREATE INDEX IF NOT EXISTS idx_settings_snapshots_rollback_source ON settings_snapshots(rollback_source_snapshot_id)",
        ),
    ),
    Migration(
        19,
        "加入照片視覺方向校正與人工設定",
        (
            "ALTER TABLE photos ADD COLUMN exif_orientation_original INTEGER",
            "ALTER TABLE photos ADD COLUMN visual_orientation_rotation_cw INTEGER CHECK(visual_orientation_rotation_cw IN (0,90,180,270) OR visual_orientation_rotation_cw IS NULL)",
            "ALTER TABLE photos ADD COLUMN visual_orientation_confidence REAL CHECK(visual_orientation_confidence IS NULL OR visual_orientation_confidence BETWEEN 0 AND 1)",
            "ALTER TABLE photos ADD COLUMN visual_orientation_ambiguous INTEGER NOT NULL DEFAULT 1 CHECK(visual_orientation_ambiguous IN (0,1))",
            "ALTER TABLE photos ADD COLUMN visual_orientation_evidence_json TEXT",
            "ALTER TABLE photos ADD COLUMN manual_orientation_rotation_cw INTEGER CHECK(manual_orientation_rotation_cw IN (0,90,180,270) OR manual_orientation_rotation_cw IS NULL)",
            "ALTER TABLE photos ADD COLUMN manual_orientation_updated_at TEXT",
            "ALTER TABLE photos ADD COLUMN manual_orientation_updated_by TEXT",
            "UPDATE photos SET exif_orientation_original=orientation WHERE exif_orientation_original IS NULL",
            "CREATE INDEX IF NOT EXISTS idx_photos_visual_orientation ON photos(manual_orientation_rotation_cw,visual_orientation_rotation_cw)",
        ),
    ),
    Migration(
        20,
        "加入可索引拍攝日期與解析狀態",
        (
            "ALTER TABLE photos ADD COLUMN captured_date TEXT",
            "ALTER TABLE photos ADD COLUMN captured_month_day TEXT",
            "ALTER TABLE photos ADD COLUMN capture_date_status TEXT NOT NULL DEFAULT 'pending' CHECK(capture_date_status IN ('pending','valid','invalid','missing'))",
            "CREATE INDEX IF NOT EXISTS idx_photos_captured_date ON photos(captured_date,id)",
            "CREATE INDEX IF NOT EXISTS idx_photos_captured_month_day ON photos(captured_month_day,captured_date,id)",
            "CREATE INDEX IF NOT EXISTS idx_photos_capture_date_status ON photos(capture_date_status,id)",
        ),
    ),
    Migration(
        21,
        "加入 Webhook 冪等鍵與持久化重試 Claim",
        (
            "ALTER TABLE device_notifications ADD COLUMN webhook_idempotency_key TEXT",
            "ALTER TABLE device_notifications ADD COLUMN webhook_claimed_until TEXT",
            "UPDATE device_notifications SET webhook_idempotency_key='legacy:' || id WHERE webhook_idempotency_key IS NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_notifications_idempotency ON device_notifications(webhook_idempotency_key)",
            "CREATE INDEX IF NOT EXISTS idx_device_notifications_claim ON device_notifications(webhook_status,webhook_next_attempt_at,webhook_claimed_until,id)",
        ),
    ),
    Migration(
        22,
        "加入決策追蹤、回饋、離線佇列、保留與 Canary 發布",
        (
            "CREATE TABLE algorithm_versions (id TEXT PRIMARY KEY,algorithm_name TEXT NOT NULL,algorithm_version TEXT NOT NULL,configuration_hash TEXT NOT NULL,configuration_snapshot_json TEXT NOT NULL DEFAULT '{}',renderer_version TEXT NOT NULL,layout_strategy_version TEXT NOT NULL,pairing_strategy_version TEXT NOT NULL,scoring_strategy_version TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(algorithm_name,algorithm_version,configuration_hash))",
            "CREATE TABLE selection_decision_traces (id INTEGER PRIMARY KEY AUTOINCREMENT,trace_id TEXT NOT NULL UNIQUE,device_id TEXT REFERENCES devices(id) ON DELETE SET NULL,execution_mode TEXT NOT NULL,algorithm_version_id TEXT REFERENCES algorithm_versions(id) ON DELETE SET NULL,render_job_id TEXT,release_id TEXT REFERENCES releases(id) ON DELETE SET NULL,primary_photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,secondary_photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,layout_mode TEXT,fit_mode TEXT,candidate_count INTEGER NOT NULL DEFAULT 0,eligible_count INTEGER NOT NULL DEFAULT 0,selected_score REAL,decision_reasons_json TEXT NOT NULL DEFAULT '[]',rejection_summary_json TEXT NOT NULL DEFAULT '{}',context_snapshot_json TEXT NOT NULL DEFAULT '{}',duration_ms INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL)",
            "CREATE TABLE selection_decision_candidates (id INTEGER PRIMARY KEY AUTOINCREMENT,trace_id TEXT NOT NULL REFERENCES selection_decision_traces(trace_id) ON DELETE CASCADE,photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,rank INTEGER NOT NULL,base_score REAL,adjusted_score REAL,selected INTEGER NOT NULL DEFAULT 0,rejection_code TEXT,score_components_json TEXT NOT NULL DEFAULT '{}',UNIQUE(trace_id,rank),UNIQUE(trace_id,photo_id))",
            "CREATE TABLE photo_feedback (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT REFERENCES users(id) ON DELETE SET NULL,device_id TEXT REFERENCES devices(id) ON DELETE SET NULL,photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,decision_trace_id TEXT REFERENCES selection_decision_traces(trace_id) ON DELETE SET NULL,feedback_type TEXT NOT NULL,value REAL NOT NULL DEFAULT 1,expires_at TEXT,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,device_id,photo_id,feedback_type))",
            "CREATE TABLE photo_pair_feedback (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT,device_id TEXT,photo_id TEXT NOT NULL,secondary_photo_id TEXT NOT NULL,decision_trace_id TEXT,feedback_type TEXT NOT NULL,value REAL NOT NULL DEFAULT 1,expires_at TEXT,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,device_id,photo_id,secondary_photo_id,feedback_type))",
            "CREATE TABLE layout_feedback (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT,device_id TEXT,photo_id TEXT,secondary_photo_id TEXT,decision_trace_id TEXT,feedback_type TEXT NOT NULL,value REAL NOT NULL DEFAULT 1,expires_at TEXT,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,device_id,decision_trace_id,feedback_type))",
            "CREATE TABLE caption_feedback (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT,device_id TEXT,photo_id TEXT,decision_trace_id TEXT,feedback_type TEXT NOT NULL,value REAL NOT NULL DEFAULT 1,expires_at TEXT,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,device_id,decision_trace_id,feedback_type))",
            "CREATE TABLE shadow_config (id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL DEFAULT 0,algorithm_version_id TEXT,device_ids_json TEXT NOT NULL DEFAULT '[]',sample_percent INTEGER NOT NULL DEFAULT 10,daily_max_runs INTEGER NOT NULL DEFAULT 10,generate_preview INTEGER NOT NULL DEFAULT 1,preview_retention_days INTEGER NOT NULL DEFAULT 30,updated_by TEXT,updated_at TEXT NOT NULL)",
            "INSERT INTO shadow_config(id,updated_at) VALUES (1,datetime('now'))",
            "CREATE TABLE device_content_queues (device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,depth INTEGER NOT NULL DEFAULT 3,queue_version INTEGER NOT NULL DEFAULT 0,current_release_id TEXT,last_known_good_release_id TEXT,next_queued_release_id TEXT,emergency_fallback_release_id TEXT,updated_at TEXT NOT NULL)",
            "CREATE TABLE device_content_queue_items (id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE RESTRICT,position INTEGER NOT NULL,priority INTEGER NOT NULL DEFAULT 100,display_after TEXT,expires_at TEXT,status TEXT NOT NULL,downloaded_at TEXT,displayed_at TEXT,retry_count INTEGER NOT NULL DEFAULT 0,last_error_code TEXT,idempotency_key TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(device_id,release_id),UNIQUE(device_id,position),UNIQUE(device_id,idempotency_key))",
            "CREATE TABLE device_content_queue_events (id INTEGER PRIMARY KEY AUTOINCREMENT,queue_item_id TEXT NOT NULL REFERENCES device_content_queue_items(id) ON DELETE CASCADE,device_id TEXT NOT NULL,event_type TEXT NOT NULL,idempotency_key TEXT,payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,UNIQUE(queue_item_id,event_type,idempotency_key))",
            "CREATE TABLE data_retention_policies (data_type TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 1,retention_days INTEGER NOT NULL,maximum_items INTEGER,maximum_bytes INTEGER,minimum_items_to_keep INTEGER NOT NULL DEFAULT 0,cleanup_batch_size INTEGER NOT NULL DEFAULT 200,dry_run INTEGER NOT NULL DEFAULT 1,last_run_at TEXT,updated_at TEXT NOT NULL)",
            "CREATE TABLE data_cleanup_runs (id TEXT PRIMARY KEY,started_at TEXT NOT NULL,completed_at TEXT,dry_run INTEGER NOT NULL,status TEXT NOT NULL,summary_json TEXT NOT NULL DEFAULT '{}',error_code TEXT)",
            "CREATE TABLE data_cleanup_items (id INTEGER PRIMARY KEY AUTOINCREMENT,cleanup_run_id TEXT NOT NULL REFERENCES data_cleanup_runs(id) ON DELETE CASCADE,data_type TEXT NOT NULL,reference_id TEXT NOT NULL,action TEXT NOT NULL,bytes_freed INTEGER NOT NULL DEFAULT 0,result TEXT NOT NULL,created_at TEXT NOT NULL)",
            "CREATE TABLE rollout_campaigns (id TEXT PRIMARY KEY,release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE RESTRICT,name TEXT NOT NULL,status TEXT NOT NULL,previous_release_id TEXT,config_json TEXT NOT NULL DEFAULT '{}',created_by TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,rollback_reason TEXT)",
            "CREATE TABLE rollout_stages (id INTEGER PRIMARY KEY AUTOINCREMENT,rollout_id TEXT NOT NULL REFERENCES rollout_campaigns(id) ON DELETE CASCADE,stage_number INTEGER NOT NULL,target_percent INTEGER NOT NULL,minimum_observation_minutes INTEGER NOT NULL DEFAULT 30,minimum_successful_devices INTEGER NOT NULL DEFAULT 1,maximum_failure_rate REAL NOT NULL DEFAULT .1,maximum_timeout_rate REAL NOT NULL DEFAULT .1,minimum_ack_rate REAL NOT NULL DEFAULT .9,manual_approval_required INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',started_at TEXT,completed_at TEXT,UNIQUE(rollout_id,stage_number))",
            "CREATE TABLE rollout_targets (id INTEGER PRIMARY KEY AUTOINCREMENT,rollout_id TEXT NOT NULL REFERENCES rollout_campaigns(id) ON DELETE CASCADE,device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,status TEXT NOT NULL DEFAULT 'pending',queue_item_id TEXT REFERENCES device_content_queue_items(id) ON DELETE SET NULL,last_error_code TEXT,updated_at TEXT NOT NULL,UNIQUE(rollout_id,device_id))",
            "CREATE TABLE rollout_health_events (id INTEGER PRIMARY KEY AUTOINCREMENT,rollout_id TEXT NOT NULL REFERENCES rollout_campaigns(id) ON DELETE CASCADE,device_id TEXT,event_type TEXT NOT NULL,severity TEXT NOT NULL,error_code TEXT,details_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL)",
            "CREATE TABLE rollout_actions (id INTEGER PRIMARY KEY AUTOINCREMENT,rollout_id TEXT NOT NULL REFERENCES rollout_campaigns(id) ON DELETE CASCADE,actor_id TEXT,action TEXT NOT NULL,reason TEXT,details_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL)",
            "CREATE INDEX idx_queue_items_device_status_position ON device_content_queue_items(device_id,status,position)",
            "CREATE INDEX idx_decision_traces_mode_created ON selection_decision_traces(execution_mode,created_at DESC,id DESC)",
            "INSERT INTO data_retention_policies(data_type,retention_days,minimum_items_to_keep,updated_at) VALUES ('decision_trace',180,0,datetime('now')),('decision_candidate',60,0,datetime('now')),('shadow_preview',30,0,datetime('now')),('device_event',180,0,datetime('now')),('queue_event',90,0,datetime('now')),('job_log',30,0,datetime('now'))",
        ),
    ),
    Migration(
        23,
        "修正決策關聯與韌性資料一致性",
        (
            "ALTER TABLE selection_decision_traces ADD COLUMN correlation_key TEXT",
            "CREATE INDEX idx_decision_traces_correlation ON selection_decision_traces(correlation_key,execution_mode,created_at DESC)",
            "CREATE INDEX idx_feedback_null_scope ON photo_feedback(user_id,photo_id,feedback_type) WHERE device_id IS NULL",
        ),
    ),
    Migration(
        24,
        "加入待分析選片與 Vision Input 指紋",
        (
            "ALTER TABLE photo_analysis ADD COLUMN analysis_fingerprint TEXT",
            "ALTER TABLE photo_analysis ADD COLUMN analysis_spec_json TEXT",
            "ALTER TABLE photo_analysis ADD COLUMN vision_request_fingerprint TEXT",
            "ALTER TABLE photo_analysis ADD COLUMN vision_input_spec_json TEXT",
            "ALTER TABLE ai_analysis_cache ADD COLUMN vision_request_fingerprint TEXT",
            "ALTER TABLE ai_analysis_cache ADD COLUMN vision_input_spec_json TEXT",
            "ALTER TABLE jobs ADD COLUMN selection_mode TEXT NOT NULL DEFAULT 'pending'",
            "ALTER TABLE jobs ADD COLUMN analysis_fingerprint TEXT",
            "ALTER TABLE jobs ADD COLUMN analysis_spec_json TEXT",
            "ALTER TABLE jobs ADD COLUMN force_recompute INTEGER NOT NULL DEFAULT 0 CHECK(force_recompute IN (0,1))",
            "CREATE INDEX idx_photo_analysis_photo_fingerprint ON photo_analysis(photo_id,analysis_fingerprint)",
            "CREATE INDEX idx_photo_analysis_vision_fingerprint ON photo_analysis(vision_request_fingerprint)",
            "CREATE INDEX idx_ai_cache_vision_fingerprint ON ai_analysis_cache(vision_request_fingerprint)",
            "CREATE INDEX idx_jobs_active_fingerprint ON jobs(analysis_fingerprint,status)",
            "CREATE INDEX idx_job_items_photo_status ON job_items(photo_id,status)",
            """
        UPDATE settings SET value_json=CASE key
            WHEN 'analysis.side_caption_min_chars' THEN '8'
            WHEN 'analysis.side_caption_target_chars' THEN '12'
            WHEN 'analysis.side_caption_max_chars' THEN '16' END,
            updated_at=datetime('now')
        WHERE key IN ('analysis.side_caption_min_chars','analysis.side_caption_target_chars','analysis.side_caption_max_chars')
          AND (SELECT COUNT(*) FROM settings WHERE (key='analysis.side_caption_min_chars' AND value_json='10')
               OR (key='analysis.side_caption_target_chars' AND value_json='22')
               OR (key='analysis.side_caption_max_chars' AND value_json='42'))=3
        """,
            "UPDATE settings SET value_json='200',updated_at=datetime('now') WHERE key='scanner.write_batch_size' AND value_json='500'",
        ),
    ),
    Migration(
        25,
        "加入帳號正規化與 Session 撤銷版本",
        (
            "ALTER TABLE users ADD COLUMN normalized_username TEXT",
            "UPDATE users SET normalized_username=lower(trim(username)) WHERE normalized_username IS NULL",
            "CREATE UNIQUE INDEX idx_users_normalized_username ON users(normalized_username)",
            "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1 CHECK(session_version >= 1)",
            "ALTER TABLE users ADD COLUMN disabled_at TEXT",
        ),
    ),
    Migration(
        26,
        "加入 OpenAI Batch 照片分析持久化生命週期",
        (
            "ALTER TABLE photos ADD COLUMN never_upload INTEGER NOT NULL DEFAULT 0 CHECK(never_upload IN (0,1))",
            "ALTER TABLE photos ADD COLUMN never_display INTEGER NOT NULL DEFAULT 0 CHECK(never_display IN (0,1))",
            "ALTER TABLE model_pricing ADD COLUMN batch_multiplier REAL NOT NULL DEFAULT 0.5",
            "ALTER TABLE model_pricing ADD COLUMN batch_input_per_million REAL",
            "ALTER TABLE model_pricing ADD COLUMN batch_cached_input_per_million REAL",
            "ALTER TABLE model_pricing ADD COLUMN batch_output_per_million REAL",
            "ALTER TABLE api_usage ADD COLUMN batch_id TEXT REFERENCES analysis_batches(id) ON DELETE SET NULL",
            "ALTER TABLE api_usage ADD COLUMN batch_item_id TEXT REFERENCES analysis_batch_items(id) ON DELETE SET NULL",
            "ALTER TABLE api_usage ADD COLUMN processing_mode TEXT NOT NULL DEFAULT 'sync' CHECK(processing_mode IN ('sync','batch'))",
            "ALTER TABLE api_usage ADD COLUMN request_id TEXT",
            "ALTER TABLE api_usage ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reasoning_tokens >= 0)",
            "CREATE INDEX IF NOT EXISTS idx_photos_never_upload_candidate ON photos(never_upload,lifecycle_status,eligible,sha256)",
            "CREATE INDEX IF NOT EXISTS idx_api_usage_batch ON api_usage(batch_id,batch_item_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_usage_batch_item_once ON api_usage(batch_item_id) WHERE batch_item_id IS NOT NULL",
            """
            CREATE TABLE IF NOT EXISTS analysis_batches (
                id TEXT PRIMARY KEY,
                job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                provider_id TEXT REFERENCES providers(id) ON DELETE SET NULL,
                model TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                analysis_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'preparing','uploading','upload_unknown','uploaded','submitting','submission_unknown',
                    'validating','in_progress','finalizing',
                    'import_pending','importing','completed','completed_with_errors',
                    'failed','expired','cancelling','cancelled','cleanup_pending'
                )),
                upload_attempt_id TEXT,
                submission_attempt_id TEXT,
                side_effect_version INTEGER NOT NULL DEFAULT 0 CHECK(side_effect_version >= 0),
                side_effect_lease_until TEXT,
                side_effect_owner TEXT,
                phase_started_at TEXT,
                abandon_confirmed_at TEXT,
                input_file_id TEXT,
                input_file_bytes INTEGER CHECK(input_file_bytes IS NULL OR input_file_bytes >= 0),
                remote_batch_id TEXT,
                output_file_id TEXT,
                error_file_id TEXT,
                input_file_deleted INTEGER NOT NULL DEFAULT 0 CHECK(input_file_deleted IN (0,1)),
                output_file_deleted INTEGER NOT NULL DEFAULT 0 CHECK(output_file_deleted IN (0,1)),
                error_file_deleted INTEGER NOT NULL DEFAULT 0 CHECK(error_file_deleted IN (0,1)),
                local_input_path TEXT,
                local_output_path TEXT,
                local_error_path TEXT,
                total_items INTEGER NOT NULL DEFAULT 0 CHECK(total_items >= 0),
                completed_items INTEGER NOT NULL DEFAULT 0 CHECK(completed_items >= 0),
                failed_items INTEGER NOT NULL DEFAULT 0 CHECK(failed_items >= 0),
                missing_items INTEGER NOT NULL DEFAULT 0 CHECK(missing_items >= 0),
                stale_items INTEGER NOT NULL DEFAULT 0 CHECK(stale_items >= 0),
                imported_items INTEGER NOT NULL DEFAULT 0 CHECK(imported_items >= 0),
                input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
                cached_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cached_tokens >= 0),
                output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
                reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reasoning_tokens >= 0),
                estimated_cost REAL NOT NULL DEFAULT 0 CHECK(estimated_cost >= 0),
                actual_cost REAL NOT NULL DEFAULT 0 CHECK(actual_cost >= 0),
                last_error_code TEXT,
                last_error_message TEXT,
                submitted_at TEXT,
                last_polled_at TEXT,
                completed_at TEXT,
                cleanup_completed_at TEXT,
                remote_status TEXT,
                sample_seed TEXT,
                candidate_snapshot_json TEXT NOT NULL DEFAULT '[]',
                scope TEXT NOT NULL DEFAULT 'all_eligible_missing_analysis'
                    CHECK(scope IN ('sample','all_eligible_missing_analysis','new_or_changed','manual_selection')),
                peak_rss_bytes INTEGER NOT NULL DEFAULT 0,
                cleanup_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(cleanup_status IN ('pending','partial','completed','not_required')
                        AND (cleanup_status!='not_required'
                             OR (input_file_id IS NULL AND output_file_id IS NULL AND error_file_id IS NULL))),
                cleanup_final_action TEXT NOT NULL DEFAULT 'none'
                    CHECK(cleanup_final_action IN ('none','complete','cancel','abandon','fail')),
                input_file_delete_unknown INTEGER NOT NULL DEFAULT 0 CHECK(input_file_delete_unknown IN (0,1)),
                output_file_delete_unknown INTEGER NOT NULL DEFAULT 0 CHECK(output_file_delete_unknown IN (0,1)),
                error_file_delete_unknown INTEGER NOT NULL DEFAULT 0 CHECK(error_file_delete_unknown IN (0,1)),
                cleanup_error_code TEXT,
                cleanup_error_message TEXT,
                reconciliation_error_code TEXT,
                reconciliation_error_message TEXT,
                provider_config_revision TEXT,
                provider_base_url_fingerprint TEXT,
                provider_project_id TEXT,
                provider_account_fingerprint TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_analysis_batches_poll ON analysis_batches(status,last_polled_at,updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_batches_poll_due ON analysis_batches(status,side_effect_lease_until,last_polled_at,phase_started_at,updated_at,created_at,id)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_batches_job ON analysis_batches(job_id,created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_batches_remote ON analysis_batches(remote_batch_id)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_batches_retry ON analysis_batches(status,completed_at,updated_at)",
            """
            CREATE TABLE IF NOT EXISTS analysis_batch_items (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES analysis_batches(id) ON DELETE CASCADE,
                job_item_id TEXT REFERENCES job_items(id) ON DELETE SET NULL,
                photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
                custom_id TEXT NOT NULL UNIQUE,
                content_sha256 TEXT NOT NULL,
                analysis_fingerprint TEXT NOT NULL,
                vision_request_fingerprint TEXT NOT NULL,
                vision_input_spec_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                    'pending','submitted','success','failed','missing','retry_pending',
                    'stale','schema_invalid','duplicate_custom_id','unexpected_custom_id',
                    'imported','cancelled','expired','upload_unknown','submission_unknown'
                )),
                request_id TEXT,
                http_status INTEGER,
                input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
                cached_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cached_tokens >= 0),
                output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
                reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reasoning_tokens >= 0),
                estimated_cost REAL NOT NULL DEFAULT 0 CHECK(estimated_cost >= 0),
                actual_cost REAL NOT NULL DEFAULT 0 CHECK(actual_cost >= 0),
                raw_response_json TEXT,
                error_code TEXT,
                error_message TEXT,
                imported_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_analysis_batch_items_batch_status ON analysis_batch_items(batch_id,status,updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_batch_items_photo ON analysis_batch_items(photo_id,batch_id,status)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_batch_items_job_item ON analysis_batch_items(job_item_id,batch_id,status)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_batch_items_fingerprint ON analysis_batch_items(content_sha256,vision_request_fingerprint,status)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_batch_items_retry ON analysis_batch_items(status,updated_at)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_items_active_job_item
            ON analysis_batch_items(job_item_id)
            WHERE job_item_id IS NOT NULL AND status IN (
                'pending','submitted','success','upload_unknown','submission_unknown'
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_items_active_content_request
            ON analysis_batch_items(content_sha256,vision_request_fingerprint)
            WHERE status IN ('pending','submitted','success','upload_unknown','submission_unknown')
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_batches_remote_id ON analysis_batches(remote_batch_id) WHERE remote_batch_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_batch_items_custom_id ON analysis_batch_items(custom_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_photo_analysis_batch_once ON photo_analysis(job_id,photo_id,analysis_fingerprint) WHERE analysis_source='analysis_batch' AND job_id IS NOT NULL AND analysis_fingerprint IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_photo_analysis_photo_id ON photo_analysis(photo_id,id DESC)",
        ),
    ),
    Migration(
        27,
        "加入人工 Review、Stock 相容與離線排程契約",
        (
            "ALTER TABLE photos ADD COLUMN review_taken_at TEXT",
            "ALTER TABLE photos ADD COLUMN review_date_source TEXT NOT NULL DEFAULT 'captured_at'",
            "ALTER TABLE devices ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'legacy_online' CHECK(delivery_mode IN ('legacy_online','stock_compat','inktime_offline_schedule'))",
            "ALTER TABLE devices ADD COLUMN offline_prefetch_allowed INTEGER NOT NULL DEFAULT 0 CHECK(offline_prefetch_allowed IN (0,1))",
            "ALTER TABLE devices ADD COLUMN offline_schedule_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE devices ADD COLUMN offline_schedule_version INTEGER NOT NULL DEFAULT 0 CHECK(offline_schedule_version >= 0)",
            "ALTER TABLE devices ADD COLUMN last_offline_slot TEXT",
            "ALTER TABLE devices ADD COLUMN schedule_times_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE devices ADD COLUMN prefetch_lead_minutes INTEGER NOT NULL DEFAULT 5 CHECK(prefetch_lead_minutes BETWEEN 0 AND 120)",
            "ALTER TABLE devices ADD COLUMN button_wake_action TEXT NOT NULL DEFAULT 'check_new' CHECK(button_wake_action IN ('check_new','local_next'))",
            "ALTER TABLE devices ADD COLUMN stock_endpoint_host TEXT",
            "ALTER TABLE device_content_queue_items ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'online_queue' CHECK(delivery_mode IN ('online_queue','offline_schedule'))",
            "ALTER TABLE device_content_queue_items ADD COLUMN offline_prefetch_allowed INTEGER NOT NULL DEFAULT 0 CHECK(offline_prefetch_allowed IN (0,1))",
            "ALTER TABLE device_content_queue_items ADD COLUMN offline_slot TEXT",
            "ALTER TABLE device_content_queue_items ADD COLUMN ack_deadline TEXT",
            "CREATE INDEX IF NOT EXISTS idx_photos_review_taken ON photos(review_taken_at,id)",
            "CREATE INDEX IF NOT EXISTS idx_photos_review_date_source ON photos(review_date_source,review_taken_at,id)",
            "CREATE INDEX IF NOT EXISTS idx_devices_delivery_mode ON devices(delivery_mode,enabled,id)",
            "CREATE INDEX IF NOT EXISTS idx_queue_items_offline_slot ON device_content_queue_items(device_id,delivery_mode,offline_slot,status)",
            "CREATE INDEX IF NOT EXISTS idx_devices_schedule_times ON devices(delivery_mode,enabled,prefetch_lead_minutes,id)",
            """
            UPDATE photos
            SET review_taken_at=COALESCE(captured_at,created_at),
                review_date_source=CASE WHEN captured_at IS NULL THEN 'created_at' ELSE 'captured_at' END
            WHERE review_taken_at IS NULL
            """,
            """
            UPDATE devices
            SET schedule_times_json=CASE
                    WHEN schedule_times_json='[]' THEN json_array(COALESCE(schedule,'08:00'))
                    ELSE schedule_times_json
                END,
                offline_schedule_json=CASE
                    WHEN offline_schedule_json='[]' THEN json_array(COALESCE(schedule,'08:00'))
                    ELSE offline_schedule_json
                END
            """,
            """
            CREATE TABLE IF NOT EXISTS device_offline_schedules (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                target_date TEXT NOT NULL,
                config_version INTEGER NOT NULL,
                timezone TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('preparing','ready','failed','cancelled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(device_id,target_date,config_version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS device_offline_schedule_slots (
                id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL REFERENCES device_offline_schedules(id) ON DELETE CASCADE,
                slot_index INTEGER NOT NULL CHECK(slot_index >= 0),
                show_at TEXT NOT NULL,
                release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE RESTRICT,
                queue_item_id TEXT NOT NULL REFERENCES device_content_queue_items(id) ON DELETE RESTRICT,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(schedule_id,slot_index),
                UNIQUE(schedule_id,show_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_offline_schedules_device_date ON device_offline_schedules(device_id,target_date,status)",
            "CREATE INDEX IF NOT EXISTS idx_offline_schedule_slots_schedule_index ON device_offline_schedule_slots(schedule_id,slot_index)",
            """
            CREATE TABLE IF NOT EXISTS photo_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                analysis_id INTEGER REFERENCES photo_analysis(id) ON DELETE CASCADE,
                review_state TEXT NOT NULL DEFAULT 'unreviewed'
                    CHECK(review_state IN ('unreviewed','keep','exclude','needs_review')),
                caption_override TEXT,
                candidate_pool INTEGER NOT NULL DEFAULT 0 CHECK(candidate_pool IN (0,1)),
                note TEXT,
                understanding_incorrect INTEGER NOT NULL DEFAULT 0 CHECK(understanding_incorrect IN (0,1)),
                caption_bad INTEGER NOT NULL DEFAULT 0 CHECK(caption_bad IN (0,1)),
                scores_unreasonable INTEGER NOT NULL DEFAULT 0 CHECK(scores_unreasonable IN (0,1)),
                accepted_at TEXT,
                version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
                updated_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(photo_id,analysis_id)
            )
            """,
            """
            INSERT OR IGNORE INTO photo_reviews(photo_id,analysis_id,review_state,candidate_pool,version,updated_at)
            SELECT p.id,latest.id,
                   CASE
                       WHEN p.exclusion_status='manually_excluded' THEN 'exclude'
                       WHEN p.exclusion_status='pending_review' THEN 'needs_review'
                       ELSE 'unreviewed'
                   END,
                   CASE WHEN p.eligible=1 AND p.lifecycle_status='active' THEN 1 ELSE 0 END,
                   0,
                   COALESCE(p.updated_at,datetime('now'))
            FROM photos p
            JOIN (SELECT photo_id,MAX(id) AS id FROM photo_analysis GROUP BY photo_id) latest
              ON latest.photo_id=p.id
            """,
            "CREATE INDEX IF NOT EXISTS idx_photo_reviews_state ON photo_reviews(review_state,updated_at,photo_id)",
            "CREATE INDEX IF NOT EXISTS idx_photo_reviews_candidate ON photo_reviews(candidate_pool,review_state,photo_id)",
            "CREATE INDEX IF NOT EXISTS idx_photo_reviews_analysis ON photo_reviews(analysis_id,review_state,photo_id)",
            "CREATE INDEX IF NOT EXISTS idx_photo_reviews_feedback ON photo_reviews(understanding_incorrect,caption_bad,scores_unreasonable,photo_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_photo_reviews_without_analysis ON photo_reviews(photo_id) WHERE analysis_id IS NULL",
            """
            CREATE TABLE IF NOT EXISTS photo_review_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                analysis_id INTEGER REFERENCES photo_analysis(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                before_json TEXT NOT NULL DEFAULT '{}',
                after_json TEXT NOT NULL DEFAULT '{}',
                actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                client_version INTEGER,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_photo_review_events_photo ON photo_review_events(photo_id,created_at DESC,id DESC)",
            """
            CREATE TABLE IF NOT EXISTS analysis_request_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
                job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                provider TEXT,
                model TEXT,
                request_fingerprint TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('completed','ambiguous_failed','failed')),
                error_code TEXT,
                error_message TEXT,
                requires_manual_confirmation INTEGER NOT NULL DEFAULT 0 CHECK(requires_manual_confirmation IN (0,1)),
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_analysis_request_outcomes_photo ON analysis_request_outcomes(photo_id,created_at DESC,id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_request_outcomes_fingerprint ON analysis_request_outcomes(request_fingerprint,created_at DESC)",
        ),
    ),
    Migration(
        28,
        "加入離線排程不可變快照、佇列歸屬與延遲 ACK 保留",
        (
            # Migration 27 schedules were built from live device settings.  A
            # ready row from that version is therefore never trusted as a
            # device-consumable schedule.
            "ALTER TABLE device_offline_schedules ADD COLUMN panel_profile TEXT NOT NULL DEFAULT 'safe_4c'",
            "ALTER TABLE device_offline_schedules ADD COLUMN rotation INTEGER NOT NULL DEFAULT 0 CHECK(rotation IN (0,180))",
            "ALTER TABLE device_offline_schedules ADD COLUMN schedule_times_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE device_offline_schedules ADD COLUMN prefetch_lead_minutes INTEGER NOT NULL DEFAULT 5 CHECK(prefetch_lead_minutes BETWEEN 0 AND 120)",
            "ALTER TABLE device_offline_schedules ADD COLUMN button_wake_action TEXT NOT NULL DEFAULT 'check_new' CHECK(button_wake_action IN ('check_new','local_next'))",
            "ALTER TABLE device_offline_schedules ADD COLUMN offline_schedule_version INTEGER NOT NULL DEFAULT 0 CHECK(offline_schedule_version >= 0)",
            "ALTER TABLE device_offline_schedules ADD COLUMN snapshot_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE device_content_queue_items ADD COLUMN offline_schedule_id TEXT REFERENCES device_offline_schedules(id) ON DELETE SET NULL",
            "ALTER TABLE device_content_queue_items ADD COLUMN terminal_ack_retention TEXT",
            "CREATE INDEX IF NOT EXISTS idx_queue_items_offline_schedule_status ON device_content_queue_items(device_id,offline_schedule_id,status)",
            "CREATE INDEX IF NOT EXISTS idx_offline_schedules_exact_ready ON device_offline_schedules(device_id,target_date,config_version,status,updated_at)",
            "CREATE TABLE IF NOT EXISTS device_offline_prefetch_cursors (id INTEGER PRIMARY KEY CHECK(id=1),last_device_id TEXT,updated_at TEXT NOT NULL)",
            "INSERT OR IGNORE INTO device_offline_prefetch_cursors(id,updated_at) VALUES (1,datetime('now'))",
            "UPDATE device_offline_schedules SET status='cancelled',updated_at=datetime('now') WHERE status='ready'",
            """
            UPDATE device_content_queue_items
            SET offline_schedule_id=(SELECT schedule_id FROM device_offline_schedule_slots s WHERE s.queue_item_id=device_content_queue_items.id)
            WHERE delivery_mode='offline_schedule' AND offline_schedule_id IS NULL
            """,
            """
            UPDATE device_content_queue_items
            SET status='CANCELLED',updated_at=datetime('now')
            WHERE delivery_mode='offline_schedule' AND offline_schedule_id IN (SELECT id FROM device_offline_schedules WHERE status='cancelled')
              AND status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')
            """,
            """
            UPDATE device_content_queue_items
            SET status='CANCELLED',updated_at=datetime('now')
            WHERE delivery_mode='offline_schedule' AND offline_schedule_id IS NULL
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_queue_offline_schedule_owner_insert
            BEFORE INSERT ON device_content_queue_items
            WHEN NEW.delivery_mode='offline_schedule' AND (NEW.offline_schedule_id IS NULL OR NOT EXISTS(
                SELECT 1 FROM device_offline_schedules s WHERE s.id=NEW.offline_schedule_id AND s.device_id=NEW.device_id))
            BEGIN SELECT RAISE(ABORT,'QUEUE-005 offline queue item requires matching offline_schedule_id'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_queue_offline_schedule_owner_update
            BEFORE UPDATE OF delivery_mode,offline_schedule_id,device_id ON device_content_queue_items
            WHEN NEW.delivery_mode='offline_schedule' AND (NEW.offline_schedule_id IS NULL OR NOT EXISTS(
                SELECT 1 FROM device_offline_schedules s WHERE s.id=NEW.offline_schedule_id AND s.device_id=NEW.device_id))
            BEGIN SELECT RAISE(ABORT,'QUEUE-005 offline queue item requires matching offline_schedule_id'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_queue_online_schedule_owner
            BEFORE INSERT ON device_content_queue_items
            WHEN NEW.delivery_mode='online_queue' AND NEW.offline_schedule_id IS NOT NULL
            BEGIN SELECT RAISE(ABORT,'QUEUE-005 online queue item cannot own offline schedule'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_queue_online_schedule_owner_update
            BEFORE UPDATE OF delivery_mode,offline_schedule_id ON device_content_queue_items
            WHEN NEW.delivery_mode='online_queue' AND NEW.offline_schedule_id IS NOT NULL
            BEGIN SELECT RAISE(ABORT,'QUEUE-005 online queue item cannot own offline schedule'); END
            """,
            # A NULL analysis_id row is the durable photo-level human
            # decision.  Existing analysis-scoped rows remain feedback for
            # that analysis and are copied only when they carry a human state.
            """
            INSERT OR IGNORE INTO photo_reviews(photo_id,analysis_id,review_state,caption_override,candidate_pool,note,understanding_incorrect,caption_bad,scores_unreasonable,accepted_at,version,updated_by,updated_at)
            SELECT legacy.photo_id,NULL,legacy.review_state,legacy.caption_override,legacy.candidate_pool,legacy.note,0,0,0,legacy.accepted_at,legacy.version,legacy.updated_by,legacy.updated_at
            FROM photo_reviews legacy
            WHERE (legacy.review_state IN ('keep','exclude','needs_review') OR legacy.candidate_pool=1)
              AND legacy.id=(
                  SELECT MAX(latest.id) FROM photo_reviews latest
                  WHERE latest.photo_id=legacy.photo_id
                    AND (latest.review_state IN ('keep','exclude','needs_review') OR latest.candidate_pool=1)
              )
            """,
            "CREATE INDEX IF NOT EXISTS idx_photo_reviews_photo_level ON photo_reviews(photo_id,analysis_id,version DESC)",
        ),
    ),
    Migration(
        29,
        "修正離線排程佇列外鍵刪除策略並保留既有資料",
        (
            # Migration 28 used ON DELETE SET NULL for the queue item's
            # schedule owner while the ownership trigger required that owner
            # to remain non-NULL.  Rebuild the queue and its three dependent
            # tables together so the stricter RESTRICT contract is real at
            # the SQLite FK layer, without changing any prior migration.
            "PRAGMA defer_foreign_keys=ON",
            "DROP TRIGGER IF EXISTS trg_queue_offline_schedule_owner_insert",
            "DROP TRIGGER IF EXISTS trg_queue_offline_schedule_owner_update",
            "DROP TRIGGER IF EXISTS trg_queue_online_schedule_owner",
            "DROP TRIGGER IF EXISTS trg_queue_online_schedule_owner_update",
            """
            CREATE TABLE device_content_queue_items_v29 (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE RESTRICT,
                position INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                display_after TEXT,
                expires_at TEXT,
                status TEXT NOT NULL,
                downloaded_at TEXT,
                displayed_at TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT,
                idempotency_key TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivery_mode TEXT NOT NULL DEFAULT 'online_queue'
                    CHECK(delivery_mode IN ('online_queue','offline_schedule')),
                offline_prefetch_allowed INTEGER NOT NULL DEFAULT 0
                    CHECK(offline_prefetch_allowed IN (0,1)),
                offline_slot TEXT,
                ack_deadline TEXT,
                offline_schedule_id TEXT REFERENCES device_offline_schedules(id) ON DELETE RESTRICT,
                terminal_ack_retention TEXT,
                UNIQUE(device_id,release_id),
                UNIQUE(device_id,position),
                UNIQUE(device_id,idempotency_key)
            )
            """,
            """
            INSERT INTO device_content_queue_items_v29(
                id,device_id,release_id,position,priority,display_after,expires_at,status,
                downloaded_at,displayed_at,retry_count,last_error_code,idempotency_key,created_at,updated_at,
                delivery_mode,offline_prefetch_allowed,offline_slot,ack_deadline,offline_schedule_id,
                terminal_ack_retention
            )
            SELECT
                id,device_id,release_id,position,priority,display_after,expires_at,status,
                downloaded_at,displayed_at,retry_count,last_error_code,idempotency_key,created_at,updated_at,
                delivery_mode,offline_prefetch_allowed,offline_slot,ack_deadline,offline_schedule_id,
                terminal_ack_retention
            FROM device_content_queue_items
            """,
            """
            CREATE TABLE device_content_queue_events_v29 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_item_id TEXT NOT NULL REFERENCES device_content_queue_items_v29(id) ON DELETE CASCADE,
                device_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                idempotency_key TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(queue_item_id,event_type,idempotency_key)
            )
            """,
            """
            INSERT INTO device_content_queue_events_v29(
                id,queue_item_id,device_id,event_type,idempotency_key,payload_json,created_at
            )
            SELECT id,queue_item_id,device_id,event_type,idempotency_key,payload_json,created_at
            FROM device_content_queue_events
            """,
            """
            CREATE TABLE device_offline_schedule_slots_v29 (
                id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL REFERENCES device_offline_schedules(id) ON DELETE CASCADE,
                slot_index INTEGER NOT NULL CHECK(slot_index >= 0),
                show_at TEXT NOT NULL,
                release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE RESTRICT,
                queue_item_id TEXT NOT NULL REFERENCES device_content_queue_items_v29(id) ON DELETE RESTRICT,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(schedule_id,slot_index),
                UNIQUE(schedule_id,show_at)
            )
            """,
            """
            INSERT INTO device_offline_schedule_slots_v29(
                id,schedule_id,slot_index,show_at,release_id,queue_item_id,sha256,created_at
            )
            SELECT id,schedule_id,slot_index,show_at,release_id,queue_item_id,sha256,created_at
            FROM device_offline_schedule_slots
            """,
            """
            CREATE TABLE rollout_targets_v29 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rollout_id TEXT NOT NULL REFERENCES rollout_campaigns(id) ON DELETE CASCADE,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                queue_item_id TEXT REFERENCES device_content_queue_items_v29(id) ON DELETE SET NULL,
                last_error_code TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(rollout_id,device_id)
            )
            """,
            """
            INSERT INTO rollout_targets_v29(
                id,rollout_id,device_id,status,queue_item_id,last_error_code,updated_at
            )
            SELECT id,rollout_id,device_id,status,queue_item_id,last_error_code,updated_at
            FROM rollout_targets
            """,
            "DROP TABLE device_content_queue_events",
            "DROP TABLE device_offline_schedule_slots",
            "DROP TABLE rollout_targets",
            "DROP TABLE device_content_queue_items",
            "ALTER TABLE device_content_queue_items_v29 RENAME TO device_content_queue_items",
            "ALTER TABLE device_content_queue_events_v29 RENAME TO device_content_queue_events",
            "ALTER TABLE device_offline_schedule_slots_v29 RENAME TO device_offline_schedule_slots",
            "ALTER TABLE rollout_targets_v29 RENAME TO rollout_targets",
            "CREATE INDEX idx_queue_items_device_status_position ON device_content_queue_items(device_id,status,position)",
            "CREATE INDEX idx_queue_items_offline_slot ON device_content_queue_items(device_id,delivery_mode,offline_slot,status)",
            "CREATE INDEX idx_queue_items_offline_schedule_status ON device_content_queue_items(device_id,offline_schedule_id,status)",
            "CREATE INDEX idx_offline_schedule_slots_schedule_index ON device_offline_schedule_slots(schedule_id,slot_index)",
            """
            CREATE TRIGGER trg_queue_offline_schedule_owner_insert
            BEFORE INSERT ON device_content_queue_items
            WHEN NEW.delivery_mode='offline_schedule' AND (NEW.offline_schedule_id IS NULL OR NOT EXISTS(
                SELECT 1 FROM device_offline_schedules s WHERE s.id=NEW.offline_schedule_id AND s.device_id=NEW.device_id))
            BEGIN SELECT RAISE(ABORT,'QUEUE-005 offline queue item requires matching offline_schedule_id'); END
            """,
            """
            CREATE TRIGGER trg_queue_offline_schedule_owner_update
            BEFORE UPDATE OF delivery_mode,offline_schedule_id,device_id ON device_content_queue_items
            WHEN NEW.delivery_mode='offline_schedule' AND (NEW.offline_schedule_id IS NULL OR NOT EXISTS(
                SELECT 1 FROM device_offline_schedules s WHERE s.id=NEW.offline_schedule_id AND s.device_id=NEW.device_id))
            BEGIN SELECT RAISE(ABORT,'QUEUE-005 offline queue item requires matching offline_schedule_id'); END
            """,
            """
            CREATE TRIGGER trg_queue_online_schedule_owner
            BEFORE INSERT ON device_content_queue_items
            WHEN NEW.delivery_mode='online_queue' AND NEW.offline_schedule_id IS NOT NULL
            BEGIN SELECT RAISE(ABORT,'QUEUE-005 online queue item cannot own offline schedule'); END
            """,
            """
            CREATE TRIGGER trg_queue_online_schedule_owner_update
            BEFORE UPDATE OF delivery_mode,offline_schedule_id ON device_content_queue_items
            WHEN NEW.delivery_mode='online_queue' AND NEW.offline_schedule_id IS NOT NULL
            BEGIN SELECT RAISE(ABORT,'QUEUE-005 online queue item cannot own offline schedule'); END
            """,
        ),
    ),
    Migration(
        30,
        "加入顯示指標時間並隔離裝置交付模式",
        (
            # Pointer timestamps make DISPLAY_COMPLETED ordering explicit;
            # NULL is retained for legacy rows whose historical ordering is
            # not trustworthy.
            "ALTER TABLE device_content_queues ADD COLUMN current_displayed_at TEXT",
            "ALTER TABLE device_content_queues ADD COLUMN last_known_good_displayed_at TEXT",
            # Close any incompatible active rows that pre-date the central
            # delivery-mode guard, while preserving an auditable queue event.
            """
            INSERT OR IGNORE INTO device_content_queue_events(
                queue_item_id,device_id,event_type,idempotency_key,payload_json,created_at
            )
            SELECT qi.id,qi.device_id,'DELIVERY_MODE_TRANSITION_CANCELLED',
                   'migration30:delivery-mode:' || qi.id,
                   '{"reason":"migration30_incompatible_delivery_mode"}',datetime('now')
            FROM device_content_queue_items qi
            JOIN devices d ON d.id=qi.device_id
            WHERE qi.status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')
              AND (
                  (d.delivery_mode='inktime_offline_schedule' AND qi.delivery_mode='online_queue')
                  OR (d.delivery_mode<>'inktime_offline_schedule' AND qi.delivery_mode='offline_schedule')
              )
            """,
            """
            UPDATE device_content_queues
            SET queue_version=queue_version + (
                    SELECT COUNT(*)
                    FROM device_content_queue_items qi
                    JOIN devices d ON d.id=qi.device_id
                    WHERE qi.device_id=device_content_queues.device_id
                      AND qi.status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')
                      AND (
                          (d.delivery_mode='inktime_offline_schedule' AND qi.delivery_mode='online_queue')
                          OR (d.delivery_mode<>'inktime_offline_schedule' AND qi.delivery_mode='offline_schedule')
                      )
                ),
                updated_at=CASE WHEN EXISTS(
                    SELECT 1
                    FROM device_content_queue_items qi
                    JOIN devices d ON d.id=qi.device_id
                    WHERE qi.device_id=device_content_queues.device_id
                      AND qi.status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')
                      AND (
                          (d.delivery_mode='inktime_offline_schedule' AND qi.delivery_mode='online_queue')
                          OR (d.delivery_mode<>'inktime_offline_schedule' AND qi.delivery_mode='offline_schedule')
                      )
                ) THEN datetime('now') ELSE updated_at END
            WHERE EXISTS(
                SELECT 1
                FROM device_content_queue_items qi
                JOIN devices d ON d.id=qi.device_id
                WHERE qi.device_id=device_content_queues.device_id
                  AND qi.status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')
                  AND (
                      (d.delivery_mode='inktime_offline_schedule' AND qi.delivery_mode='online_queue')
                      OR (d.delivery_mode<>'inktime_offline_schedule' AND qi.delivery_mode='offline_schedule')
                  )
            )
            """,
            """
            UPDATE device_content_queue_items
            SET status='CANCELLED',last_error_code='QUEUE-005',updated_at=datetime('now')
            WHERE status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')
              AND EXISTS(
                  SELECT 1 FROM devices d
                  WHERE d.id=device_content_queue_items.device_id
                    AND (
                        (d.delivery_mode='inktime_offline_schedule' AND device_content_queue_items.delivery_mode='online_queue')
                        OR (d.delivery_mode<>'inktime_offline_schedule' AND device_content_queue_items.delivery_mode='offline_schedule')
                    )
              )
            """,
            """
            UPDATE rollout_targets
            SET status='cancelled_mode_transition',last_error_code='QUEUE-005',updated_at=datetime('now')
            WHERE queue_item_id IN (
                SELECT queue_item_id
                FROM device_content_queue_events
                WHERE event_type='DELIVERY_MODE_TRANSITION_CANCELLED'
                  AND idempotency_key LIKE 'migration30:delivery-mode:%'
            )
            """,
            """
            CREATE TRIGGER trg_queue_device_delivery_mode_insert
            BEFORE INSERT ON device_content_queue_items
            WHEN (
                NEW.delivery_mode='online_queue' AND EXISTS(
                    SELECT 1 FROM devices d
                    WHERE d.id=NEW.device_id AND d.delivery_mode='inktime_offline_schedule'
                )
            ) OR (
                NEW.delivery_mode='offline_schedule' AND NOT EXISTS(
                    SELECT 1 FROM devices d
                    WHERE d.id=NEW.device_id
                      AND d.delivery_mode='inktime_offline_schedule'
                      AND d.offline_prefetch_allowed=1
                )
            )
            BEGIN SELECT RAISE(ABORT,'QUEUE-005 queue delivery mode incompatible with device'); END
            """,
            """
            CREATE TRIGGER trg_queue_device_delivery_mode_update
            BEFORE UPDATE OF delivery_mode,device_id ON device_content_queue_items
            WHEN (
                NEW.delivery_mode='online_queue' AND EXISTS(
                    SELECT 1 FROM devices d
                    WHERE d.id=NEW.device_id AND d.delivery_mode='inktime_offline_schedule'
                )
            ) OR (
                NEW.delivery_mode='offline_schedule' AND NOT EXISTS(
                    SELECT 1 FROM devices d
                    WHERE d.id=NEW.device_id
                      AND d.delivery_mode='inktime_offline_schedule'
                      AND d.offline_prefetch_allowed=1
                )
            )
            BEGIN SELECT RAISE(ABORT,'QUEUE-005 queue delivery mode incompatible with device'); END
            """,
        ),
    ),
    Migration(
        31,
        "固定裝置交付模式與離線預取契約",
        (
            # Repair legacy rows before installing the trigger so an existing
            # database can upgrade atomically without losing queue history.
            """
            UPDATE devices
            SET offline_prefetch_allowed=CASE
                    WHEN delivery_mode='inktime_offline_schedule' THEN 1
                    ELSE 0
                END,
                updated_at=datetime('now')
            WHERE offline_prefetch_allowed != CASE
                    WHEN delivery_mode='inktime_offline_schedule' THEN 1
                    ELSE 0
                END
            """,
            """
            CREATE TRIGGER trg_device_delivery_prefetch_insert
            BEFORE INSERT ON devices
            WHEN (NEW.delivery_mode='inktime_offline_schedule' AND NEW.offline_prefetch_allowed<>1)
              OR (NEW.delivery_mode IN ('legacy_online','stock_compat') AND NEW.offline_prefetch_allowed<>0)
            BEGIN
                SELECT RAISE(ABORT,'DEVICE-008 delivery_mode 與 offline_prefetch_allowed 不一致');
            END
            """,
            """
            CREATE TRIGGER trg_device_delivery_prefetch_update
            BEFORE UPDATE OF delivery_mode,offline_prefetch_allowed ON devices
            WHEN (NEW.delivery_mode='inktime_offline_schedule' AND NEW.offline_prefetch_allowed<>1)
              OR (NEW.delivery_mode IN ('legacy_online','stock_compat') AND NEW.offline_prefetch_allowed<>0)
            BEGIN
                SELECT RAISE(ABORT,'DEVICE-008 delivery_mode 與 offline_prefetch_allowed 不一致');
            END
            """,
        ),
    ),
    Migration(
        32,
        "加入 Provider 路由選項與真實成本來源",
        (
            "ALTER TABLE providers ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE api_usage ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_write_tokens >= 0)",
            "ALTER TABLE api_usage ADD COLUMN cost_source TEXT NOT NULL DEFAULT 'unknown' CHECK(cost_source IN ('provider_reported','estimated','unknown'))",
            "ALTER TABLE api_usage ADD COLUMN prompt_chars INTEGER NOT NULL DEFAULT 0 CHECK(prompt_chars >= 0)",
            "ALTER TABLE api_usage ADD COLUMN schema_chars INTEGER NOT NULL DEFAULT 0 CHECK(schema_chars >= 0)",
            "ALTER TABLE api_usage ADD COLUMN request_body_bytes INTEGER NOT NULL DEFAULT 0 CHECK(request_body_bytes >= 0)",
            "ALTER TABLE api_usage ADD COLUMN image_bytes INTEGER NOT NULL DEFAULT 0 CHECK(image_bytes >= 0)",
            # Before Migration 32 the synchronous recorder stored its local
            # estimate in both estimated_cost and actual_cost.  Those legacy
            # actual_cost values have no provider provenance and must never be
            # relabeled as provider-reported money.
            "UPDATE api_usage SET cost_source=CASE WHEN COALESCE(estimated_cost,0) > 0 OR COALESCE(actual_cost,0) > 0 THEN 'estimated' ELSE 'unknown' END",
            "CREATE INDEX IF NOT EXISTS idx_api_usage_cost_source ON api_usage(cost_source,started_at)",
        ),
    ),
    Migration(
        33,
        "修復 Provider 身分、OpenRouter 相容列與成本回溯索引",
        (
            "ALTER TABLE api_usage ADD COLUMN provider_id TEXT REFERENCES providers(id) ON DELETE SET NULL",
            """
            UPDATE api_usage
            SET provider_id=(
                SELECT p.id FROM providers AS p
                WHERE p.name=api_usage.provider
            )
            WHERE provider_id IS NULL
              AND (SELECT COUNT(*) FROM providers AS p WHERE p.name=api_usage.provider)=1
            """,
            "CREATE INDEX IF NOT EXISTS idx_api_usage_provider_model_cost_time ON api_usage(provider_id,model,cost_source,started_at)",
            "CREATE INDEX IF NOT EXISTS idx_api_usage_job_cost_source ON api_usage(job_id,cost_source)",
            "CREATE INDEX IF NOT EXISTS idx_api_usage_photo_cost_source ON api_usage(photo_id,cost_source)",
        ),
    ),
    Migration(
        34,
        "加入自動裝置配對與版本化 credential 生命週期",
        (
            "ALTER TABLE devices ADD COLUMN auth_mode TEXT NOT NULL DEFAULT 'legacy_token' CHECK(auth_mode IN ('automatic','legacy_token','stock'))",
            "ALTER TABLE devices ADD COLUMN pairing_state TEXT NOT NULL DEFAULT 'paired' CHECK(pairing_state IN ('unpaired','pairing_pending','paired','revoked','auth_invalid','pairing_expired'))",
            "ALTER TABLE devices ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 0 CHECK(credential_version >= 0)",
            "ALTER TABLE devices ADD COLUMN device_secret_hash TEXT",
            "ALTER TABLE devices ADD COLUMN paired_at TEXT",
            "ALTER TABLE devices ADD COLUMN last_auth_at TEXT",
            "ALTER TABLE devices ADD COLUMN auth_revoked_at TEXT",
            "ALTER TABLE devices ADD COLUMN pairing_code_hash TEXT",
            "ALTER TABLE devices ADD COLUMN pairing_expires_at TEXT",
            "ALTER TABLE devices ADD COLUMN pairing_attempts INTEGER NOT NULL DEFAULT 0 CHECK(pairing_attempts >= 0)",
            "ALTER TABLE devices ADD COLUMN pairing_claim_attempts INTEGER NOT NULL DEFAULT 0 CHECK(pairing_claim_attempts >= 0)",
            "ALTER TABLE devices ADD COLUMN pairing_requested_at TEXT",
            "ALTER TABLE devices ADD COLUMN firmware_identity TEXT",
            "ALTER TABLE devices ADD COLUMN previous_device_secret_hash TEXT",
            "ALTER TABLE devices ADD COLUMN previous_credential_version INTEGER CHECK(previous_credential_version IS NULL OR previous_credential_version >= 0)",
            "ALTER TABLE devices ADD COLUMN previous_credential_expires_at TEXT",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_device_secret_hash ON devices(device_secret_hash) WHERE device_secret_hash IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_previous_device_secret_hash ON devices(previous_device_secret_hash) WHERE previous_device_secret_hash IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_devices_auth_state ON devices(auth_mode,pairing_state,enabled,id)",
            """
            CREATE TABLE IF NOT EXISTS device_pairing_requests (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                pairing_nonce_hash TEXT NOT NULL,
                pairing_code_hash TEXT NOT NULL,
                pairing_code_ciphertext BLOB NOT NULL,
                firmware_identity TEXT NOT NULL DEFAULT '',
                firmware_version TEXT,
                panel_profile TEXT,
                capabilities_json TEXT NOT NULL DEFAULT '{}',
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                claim_attempts INTEGER NOT NULL DEFAULT 0 CHECK(claim_attempts >= 0),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','approved','rejected','expired','consumed')),
                requested_at TEXT NOT NULL,
                approved_at TEXT,
                approved_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                consumed_at TEXT,
                UNIQUE(device_id,id)
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_pairing_active_device ON device_pairing_requests(device_id) WHERE status IN ('pending','approved')",
            "CREATE INDEX IF NOT EXISTS idx_device_pairing_status_expiry ON device_pairing_requests(status,expires_at,id)",
            """
            CREATE TABLE IF NOT EXISTS device_pairing_rate_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL CHECK(scope IN ('ip','device')),
                scope_hash TEXT NOT NULL,
                attempted_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_device_pairing_rate_scope_time ON device_pairing_rate_limits(scope,scope_hash,attempted_at)",
            # Existing rows keep their deployed legacy behavior.  Stock rows
            # are explicitly separated so the new firmware pairing flow can
            # never be reached through the Stock compatibility path.
            "UPDATE devices SET auth_mode=CASE WHEN delivery_mode='stock_compat' THEN 'stock' ELSE 'legacy_token' END, pairing_state='paired', credential_version=0, updated_at=updated_at",
        ),
    ),
    Migration(
        35,
        "收斂實體配對 possession、可恢復 claim 與 repair permission",
        (
            "ALTER TABLE devices ADD COLUMN repair_allowed_until TEXT",
            # Graceful rotation was never exposed as a complete protocol.  The
            # supported lifecycle is revoke -> explicit repair -> new secret.
            "UPDATE devices SET previous_device_secret_hash=NULL, previous_credential_version=NULL, previous_credential_expires_at=NULL, repair_allowed_until=NULL",
            # Migration 34 could create enabled, credential-less automatic rows
            # before an ESP32 enrolled.  They are not usable devices and must
            # not remain enabled while the new pending-enrollment model is used.
            "UPDATE devices SET enabled=0 WHERE auth_mode='automatic' AND pairing_state IN ('unpaired','pairing_pending','pairing_expired')",
            "DROP INDEX IF EXISTS idx_device_pairing_active_device",
            "DROP INDEX IF EXISTS idx_device_pairing_status_expiry",
            "ALTER TABLE device_pairing_requests RENAME TO device_pairing_requests_v34",
            """
            CREATE TABLE device_pairing_requests (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                pairing_nonce_hash TEXT NOT NULL,
                pairing_code_hash TEXT NOT NULL,
                firmware_identity TEXT NOT NULL DEFAULT '',
                firmware_version TEXT,
                panel_profile TEXT,
                capabilities_json TEXT NOT NULL DEFAULT '{}',
                config_json TEXT NOT NULL DEFAULT '{}',
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                claim_attempts INTEGER NOT NULL DEFAULT 0 CHECK(claim_attempts >= 0),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','approved','credential_issued','confirmed','rejected','expired')),
                requested_at TEXT NOT NULL,
                approved_at TEXT,
                approved_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                credential_issued_at TEXT,
                credential_envelope_ciphertext BLOB,
                credential_envelope_expires_at TEXT,
                confirmed_at TEXT,
                UNIQUE(device_id,id)
            )
            """,
            """
            INSERT INTO device_pairing_requests(
                id,device_id,pairing_nonce_hash,pairing_code_hash,firmware_identity,
                firmware_version,panel_profile,capabilities_json,config_json,expires_at,
                attempts,claim_attempts,status,requested_at,approved_at,approved_by,
                credential_issued_at,credential_envelope_ciphertext,credential_envelope_expires_at,
                confirmed_at
            )
            SELECT
                id,device_id,pairing_nonce_hash,pairing_code_hash,firmware_identity,
                firmware_version,panel_profile,capabilities_json,'{}',expires_at,
                attempts,claim_attempts,
                CASE WHEN status IN ('pending','approved') THEN 'expired'
                     WHEN status='consumed' THEN 'confirmed'
                     ELSE status END,
                requested_at,approved_at,approved_by,NULL,NULL,NULL,
                CASE WHEN status='consumed' THEN consumed_at ELSE NULL END
            FROM device_pairing_requests_v34
            """,
            "DROP TABLE device_pairing_requests_v34",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_pairing_active_device ON device_pairing_requests(device_id) WHERE status IN ('pending','approved','credential_issued')",
            "CREATE INDEX IF NOT EXISTS idx_device_pairing_status_expiry ON device_pairing_requests(status,expires_at,id)",
            "CREATE INDEX IF NOT EXISTS idx_device_pairing_nonce ON device_pairing_requests(device_id,pairing_nonce_hash)",
        ),
    ),
    Migration(
        36,
        "加入離線排程套用 ACK、循環間隔與同步策略",
        (
            "ALTER TABLE devices ADD COLUMN applied_offline_schedule_version INTEGER NOT NULL DEFAULT 0 CHECK(applied_offline_schedule_version >= 0)",
            "ALTER TABLE devices ADD COLUMN offline_schedule_ack_at TEXT",
            "ALTER TABLE devices ADD COLUMN minimum_schedule_gap_minutes INTEGER NOT NULL DEFAULT 60 CHECK(minimum_schedule_gap_minutes BETWEEN 30 AND 360)",
            "ALTER TABLE devices ADD COLUMN sync_strategy TEXT NOT NULL DEFAULT 'first_display_lead' CHECK(sync_strategy IN ('first_display_lead','fixed_daily'))",
            "ALTER TABLE devices ADD COLUMN sync_time TEXT",
            "ALTER TABLE device_offline_schedules ADD COLUMN minimum_schedule_gap_minutes INTEGER NOT NULL DEFAULT 60 CHECK(minimum_schedule_gap_minutes BETWEEN 30 AND 360)",
            "ALTER TABLE device_offline_schedules ADD COLUMN sync_strategy TEXT NOT NULL DEFAULT 'first_display_lead' CHECK(sync_strategy IN ('first_display_lead','fixed_daily'))",
            "ALTER TABLE device_offline_schedules ADD COLUMN sync_time TEXT",
            "CREATE INDEX IF NOT EXISTS idx_devices_offline_ack ON devices(delivery_mode,offline_schedule_version,applied_offline_schedule_version,id)",
            "CREATE INDEX IF NOT EXISTS idx_offline_schedules_runtime ON device_offline_schedules(device_id,target_date,status,config_version,updated_at)",
        ),
    ),
    Migration(
        37,
        "記錄離線排程終端內容結果",
        (
            "ALTER TABLE device_offline_schedules ADD COLUMN terminal_outcome_code TEXT",
            "CREATE INDEX IF NOT EXISTS idx_offline_schedules_terminal_outcome ON device_offline_schedules(device_id,target_date,config_version,terminal_outcome_code)",
        ),
    ),
    Migration(
        38,
        "記錄裝置離線排程 Slot 能力",
        (
            "ALTER TABLE devices ADD COLUMN offline_schedule_max_slots INTEGER NOT NULL DEFAULT 12 CHECK(offline_schedule_max_slots BETWEEN 1 AND 24)",
        ),
    ),
    Migration(
        39,
        "收斂離線 Slot 能力並建立預取截止時間",
        (
            # Existing schema 37 rows may already contain 13--24 schedule
            # values without a persisted hardware capability.  Preserve the
            # configuration, but quarantine it until a new pairing/repair
            # explicitly confirms the 24-slot capability.
            "ALTER TABLE devices ADD COLUMN offline_schedule_capability_state TEXT NOT NULL DEFAULT 'unknown_12' CHECK(offline_schedule_capability_state IN ('unknown_12','confirmed_24','legacy_ambiguous'))",
            "ALTER TABLE devices ADD COLUMN next_offline_prepare_at TEXT",
            "CREATE INDEX IF NOT EXISTS idx_devices_offline_prepare_due ON devices(next_offline_prepare_at,id) WHERE enabled=1 AND delivery_mode='inktime_offline_schedule' AND offline_prefetch_allowed=1 AND offline_schedule_capability_state IN ('unknown_12','confirmed_24') AND next_offline_prepare_at IS NOT NULL",
            """
            UPDATE devices
            SET offline_schedule_capability_state=CASE
                    WHEN offline_schedule_max_slots=24 THEN 'confirmed_24'
                    WHEN offline_schedule_max_slots<>12 THEN 'legacy_ambiguous'
                    WHEN json_valid(schedule_times_json)=0 THEN 'legacy_ambiguous'
                    WHEN json_type(schedule_times_json)<>'array' THEN 'legacy_ambiguous'
                    WHEN json_array_length(schedule_times_json)>12 THEN 'legacy_ambiguous'
                    WHEN json_valid(offline_schedule_json)=0 THEN 'legacy_ambiguous'
                    WHEN json_type(offline_schedule_json)<>'array' THEN 'legacy_ambiguous'
                    WHEN json_array_length(offline_schedule_json)>12 THEN 'legacy_ambiguous'
                    ELSE 'unknown_12'
                END,
                offline_schedule_max_slots=CASE
                    WHEN offline_schedule_max_slots=24 THEN 24
                    ELSE 12
                END
            """,
            # A Unix-epoch deadline is a bounded one-time initialization probe.
            # The running scheduler replaces it with a local timezone/DST-aware
            # deadline before the next normal poll.
            """
            UPDATE devices
            SET next_offline_prepare_at='1970-01-01T00:00:00+00:00'
            WHERE enabled=1
              AND delivery_mode='inktime_offline_schedule'
              AND offline_prefetch_allowed=1
              AND offline_schedule_capability_state IN ('unknown_12','confirmed_24')
            """,
            """
            INSERT INTO device_events(
                device_id,level,event,error_code,message,details_json,created_at
            )
            SELECT id,'warning','offline_schedule_capability_quarantined','DEVICE-008',
                   '舊離線排程能力不明；已隔離，請重新配對或 Repair 確認 24-slot capability',
                   json_object(
                       'offline_schedule_capability_state',offline_schedule_capability_state,
                       'offline_schedule_max_slots',offline_schedule_max_slots
                   ),datetime('now')
            FROM devices
            WHERE offline_schedule_capability_state='legacy_ambiguous'
            """,
            """
            CREATE TRIGGER trg_devices_offline_schedule_slots_insert
            BEFORE INSERT ON devices
            WHEN NEW.offline_schedule_max_slots NOT IN (12,24)
              OR NOT (
                    (NEW.offline_schedule_capability_state IN ('unknown_12','legacy_ambiguous')
                     AND NEW.offline_schedule_max_slots=12)
                 OR (NEW.offline_schedule_capability_state='confirmed_24'
                     AND NEW.offline_schedule_max_slots=24)
              )
            BEGIN
                SELECT RAISE(ABORT,'DEVICE-008 offline slot capability state is inconsistent');
            END
            """,
            """
            CREATE TRIGGER trg_devices_offline_schedule_slots_update
            BEFORE UPDATE OF offline_schedule_max_slots,offline_schedule_capability_state ON devices
            WHEN NEW.offline_schedule_max_slots NOT IN (12,24)
              OR NOT (
                    (NEW.offline_schedule_capability_state IN ('unknown_12','legacy_ambiguous')
                     AND NEW.offline_schedule_max_slots=12)
                 OR (NEW.offline_schedule_capability_state='confirmed_24'
                     AND NEW.offline_schedule_max_slots=24)
              )
            BEGIN
                SELECT RAISE(ABORT,'DEVICE-008 offline slot capability state is inconsistent');
            END
            """,
            """
            UPDATE settings
            SET value_json='21600',value_type='integer',updated_at=datetime('now')
            WHERE key='system.diagnostics_cache_seconds'
              AND value_json='300'
              AND (updated_by IS NULL OR updated_by='')
            """,
        ),
    ),
    Migration(
        40,
        "加入照片列表穩定排序索引",
        (
            "CREATE INDEX IF NOT EXISTS idx_photos_legacy_sort ON photos(COALESCE(captured_at,created_at) DESC,id DESC)",
        ),
    ),
    Migration(
        41,
        "記錄裝置狀態單調序號",
        (
            "ALTER TABLE devices ADD COLUMN last_status_sequence INTEGER",
            "CREATE INDEX IF NOT EXISTS idx_devices_status_sequence ON devices(id,last_status_sequence)",
        ),
    ),
    Migration(
        42,
        "保存昂貴 POST 的 Idempotency request fingerprint",
        (
            "ALTER TABLE jobs ADD COLUMN request_fingerprint TEXT",
            "CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_fingerprint ON jobs(dedupe_key,request_fingerprint)",
        ),
    ),
    Migration(
        43,
        "修復診斷快取歷史來源",
        (),
    ),
    Migration(
        44,
        "補回既有 legacy ambiguous 裝置的 DEVICE-008 可見警告",
        (
            """
            INSERT INTO device_events(
                device_id,level,event,error_code,message,details_json,created_at
            )
            SELECT d.id,'warning','offline_schedule_capability_quarantined','DEVICE-008',
                   '舊離線排程能力不明；已隔離，請重新配對或 Repair 確認 24-slot capability',
                   json_object(
                       'offline_schedule_capability_state',d.offline_schedule_capability_state,
                       'offline_schedule_max_slots',d.offline_schedule_max_slots
                   ),datetime('now')
            FROM devices AS d
            WHERE d.offline_schedule_capability_state='legacy_ambiguous'
              AND NOT EXISTS (
                  SELECT 1 FROM device_events AS e
                  WHERE e.device_id=d.id
                    AND e.event='offline_schedule_capability_quarantined'
                    AND e.error_code='DEVICE-008'
              )
            """,
        ),
    ),
    Migration(
        45,
        "加入 API 用量保留生命週期",
        (
            "INSERT OR IGNORE INTO data_retention_policies(data_type,enabled,retention_days,minimum_items_to_keep,cleanup_batch_size,dry_run,updated_at) VALUES ('api_usage',1,400,0,200,1,datetime('now'))",
        ),
    ),
    Migration(
        46,
        "加入完整照片庫 request-level Idempotency ledger",
        (
            """
            CREATE TABLE idempotency_requests (
                scope_key TEXT PRIMARY KEY,
                request_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('in_progress','completed')),
                request_snapshot_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_idempotency_requests_status_updated ON idempotency_requests(status,updated_at)",
        ),
    ),
    Migration(
        47,
        "加入完整照片庫 reservation ownership lease",
        (
            "ALTER TABLE idempotency_requests ADD COLUMN reservation_token TEXT",
            "ALTER TABLE idempotency_requests ADD COLUMN reservation_expires_at TEXT",
        ),
    ),
    Migration(
        48,
        "允許未知 API 成本保留 NULL",
        (),
    ),
    Migration(
        49,
        "啟用未修改的 API 用量自動保留",
        (
            """
            UPDATE data_retention_policies
            SET dry_run=0,updated_at=datetime('now')
            WHERE data_type='api_usage'
              AND enabled=1
              AND retention_days=400
              AND maximum_items IS NULL
              AND maximum_bytes IS NULL
              AND minimum_items_to_keep=0
              AND cleanup_batch_size=200
              AND dry_run=1
              AND updated_at NOT LIKE '%T%'
              AND EXISTS (
                  SELECT 1
                  FROM schema_migrations AS migration_45
                  WHERE migration_45.version=45
                    AND migration_45.name='加入 API 用量保留生命週期'
                    AND ABS(
                        (julianday(data_retention_policies.updated_at)
                         - julianday(migration_45.applied_at)) * 86400.0
                    ) <= 5.0
              )
            """,
        ),
    ),
    Migration(
        50,
        "為保留清理稽核建立有界 GC 索引",
        (
            "CREATE INDEX IF NOT EXISTS idx_data_cleanup_runs_completed ON data_cleanup_runs(completed_at,id)",
            "CREATE INDEX IF NOT EXISTS idx_data_cleanup_items_cleanup_run ON data_cleanup_items(cleanup_run_id)",
        ),
    ),
    Migration(
        51,
        "加入有界 AI 分析追蹤檢視器",
        (
            """
            CREATE TABLE ai_trace_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL UNIQUE,
                job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
                stage TEXT NOT NULL,
                prompt_version TEXT,
                analysis_fingerprint TEXT,
                vision_request_fingerprint TEXT,
                status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCESS','FAILED','TIMEOUT','AMBIGUOUS')),
                started_at TEXT NOT NULL,
                response_received_at TEXT,
                scoring_completed_at TEXT,
                result_persisted_at TEXT,
                completed_at TEXT,
                final_result_json TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_ai_trace_runs_created ON ai_trace_runs(created_at,id)",
            "CREATE INDEX idx_ai_trace_runs_photo ON ai_trace_runs(photo_id,id DESC)",
            "CREATE INDEX idx_ai_trace_runs_job ON ai_trace_runs(job_id,id DESC)",
            "CREATE INDEX idx_ai_trace_runs_status ON ai_trace_runs(status,id DESC)",
            """
            CREATE TABLE ai_trace_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL REFERENCES ai_trace_runs(trace_id) ON DELETE CASCADE,
                attempt_number INTEGER NOT NULL,
                attempt_kind TEXT NOT NULL CHECK(attempt_kind IN ('vision','json_repair')),
                provider TEXT NOT NULL,
                provider_id TEXT,
                requested_model TEXT NOT NULL,
                served_model TEXT,
                endpoint TEXT,
                api_mode TEXT,
                status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCESS','FAILED','TIMEOUT','AMBIGUOUS','VALIDATION_FAILED')),
                request_json_sanitized TEXT,
                response_raw_sanitized TEXT,
                response_parsed_json TEXT,
                request_built_at TEXT,
                request_started_at TEXT,
                response_received_at TEXT,
                parse_started_at TEXT,
                parsed_at TEXT,
                completed_at TEXT,
                http_status INTEGER,
                provider_request_id TEXT,
                api_usage_id INTEGER REFERENCES api_usage(id) ON DELETE SET NULL,
                latency_ms INTEGER,
                retry_reason TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(trace_id,attempt_number)
            )
            """,
            "CREATE INDEX idx_ai_trace_attempts_trace ON ai_trace_attempts(trace_id,attempt_number)",
            "CREATE INDEX idx_ai_trace_attempts_provider ON ai_trace_attempts(provider,id DESC)",
            "CREATE INDEX idx_ai_trace_attempts_models ON ai_trace_attempts(requested_model,served_model)",
            "INSERT INTO data_retention_policies(data_type,enabled,retention_days,maximum_items,maximum_bytes,minimum_items_to_keep,cleanup_batch_size,dry_run,updated_at) VALUES ('ai_trace',1,30,NULL,NULL,0,200,0,datetime('now'))",
        ),
    ),
    Migration(
        52,
        "加入 Provider 預設模型 ID",
        (
            "ALTER TABLE providers ADD COLUMN model TEXT",
        ),
    ),
    Migration(
        53,
        "統一既有 AI 分析為台灣繁體中文",
        (),
    ),
    Migration(
        54,
        "Vision schema v4 與本機特殊程度排序",
        (
            "ALTER TABLE photo_analysis ADD COLUMN visual_score REAL CHECK(visual_score BETWEEN 0 AND 100)",
            "ALTER TABLE photo_analysis ADD COLUMN local_quality_score REAL CHECK(local_quality_score BETWEEN 0 AND 100)",
            "ALTER TABLE photo_analysis ADD COLUMN special_level INTEGER CHECK(special_level BETWEEN 0 AND 4)",
            "ALTER TABLE photo_analysis ADD COLUMN special_codes_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE photo_analysis ADD COLUMN people_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE photo_analysis ADD COLUMN content_filter_json TEXT",
            "ALTER TABLE photo_analysis ADD COLUMN effective_special_level INTEGER",
            "ALTER TABLE photo_analysis ADD COLUMN library_rarity_adjustment INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE scoring_rule_versions RENAME COLUMN beauty_weight TO visual_weight",
            "ALTER TABLE scoring_rule_versions RENAME COLUMN technical_weight TO local_weight",
            "UPDATE scoring_rule_versions SET is_active=0",
            "DELETE FROM settings WHERE key LIKE 'analysis.ranking_%'",
            "DELETE FROM settings WHERE key='analysis.scoring_rules' AND updated_by IS NULL",
            "UPDATE settings SET value_json='30' WHERE key='analysis.caption_min_chars'",
            "UPDATE settings SET value_json='60' WHERE key='analysis.caption_target_chars'",
            "UPDATE settings SET value_json='100' WHERE key='analysis.caption_max_chars'",
            "UPDATE settings SET value_json='1200' WHERE key IN ('budget.full_analysis_max_tokens','budget.caption_variants_max_tokens') AND CAST(value_json AS INTEGER)>1200",
        ),
    ),
    Migration(
        55,
        "持久化照片庫排序待重算狀態",
        (
            """
            CREATE TABLE library_ranking_state (
                library_id TEXT PRIMARY KEY REFERENCES libraries(id) ON DELETE CASCADE,
                dirty INTEGER NOT NULL DEFAULT 1 CHECK(dirty IN (0,1)),
                updated_at TEXT NOT NULL,
                last_refreshed_at TEXT
            )
            """,
            "INSERT INTO library_ranking_state(library_id,dirty,updated_at) SELECT id,1,datetime('now') FROM libraries",
            "UPDATE settings SET value_json='10' WHERE key='analysis.caption_min_chars'",
            "UPDATE settings SET value_json='60' WHERE key='analysis.caption_target_chars'",
            "UPDATE settings SET value_json='100' WHERE key='analysis.caption_max_chars'",
        ),
    ),
    Migration(
        56,
        "分離本機品質與 Vision v4 語意排序來源",
        (
            """
            ALTER TABLE photo_analysis ADD COLUMN score_kind TEXT NOT NULL DEFAULT 'legacy'
                CHECK(score_kind IN ('semantic','local_quality','legacy'))
            """,
            """
            UPDATE photo_analysis
            SET score_kind='local_quality'
            WHERE lower(COALESCE(provider,'')) IN (
                    'local','local-prefilter','local-quality-v3','virtual-display-local'
                )
               OR lower(COALESCE(stage,'')) IN ('local','local_fallback','prefilter')
            """,
            """
            UPDATE photo_analysis
            SET score_kind='semantic'
            WHERE score_kind='legacy'
              AND schema_version=4
              AND ranking_score IS NOT NULL
              AND memory_score IS NOT NULL
              AND visual_score IS NOT NULL
              AND length(trim(COALESCE(provider,''))) > 0
              AND lower(provider) <> 'inherited'
            """,
            "CREATE INDEX IF NOT EXISTS idx_photo_analysis_score_kind_ranking ON photo_analysis(score_kind,ranking_score DESC,photo_id)",
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
    started = time.monotonic()
    log_event(
        LOGGER,
        logging.INFO,
        "Pre-migration backup started",
        event="migration_backup_started",
        phase="backup",
    )
    source = sqlite3.connect(database.path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError("升級前備份完整性檢查失敗")
    finally:
        target.close()
        source.close()
    log_event(
        LOGGER,
        logging.INFO,
        "Pre-migration backup completed",
        event="migration_backup_completed",
        phase="backup",
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return destination


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return bool(
        connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    )


def _official_openrouter_host(base_url: str) -> bool:
    try:
        hostname = (urlparse(str(base_url or "").strip()).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai")


def _apply_migration_33_data_fixes(connection: sqlite3.Connection) -> None:
    """Keep legacy rows behaviorally equivalent while the schema upgrade is atomic."""

    providers = connection.execute(
        "SELECT id,kind,base_url,options_json FROM providers"
    ).fetchall()
    for row in providers:
        raw_kind = str(row["kind"] or "").strip().lower()
        base_url = str(row["base_url"] or "")
        if raw_kind != "openai_compatible" or not _official_openrouter_host(base_url):
            continue
        try:
            options = json.loads(str(row["options_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            options = {}
        if not isinstance(options, dict):
            options = {}
        if type(options.get("require_parameters")) is not bool:
            options["require_parameters"] = True
        connection.execute(
            "UPDATE providers SET kind='openrouter',supports_batch=0,options_json=?,updated_at=datetime('now') WHERE id=?",
            (json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":")), row["id"]),
        )

    # Migration 32 cannot distinguish an old zero-activity row from an old
    # row whose pricing evidence was unavailable: both were initialized as
    # ``unknown``.  Migration 33 must preserve that historical uncertainty;
    # newly-added zero-valued metric columns do not prove that a request was
    # free.  Budget/reconciliation policy may avoid reserving a billable
    # amount when no evidence exists, but it must not rewrite the provenance.


def _migration_integrity_is_ok(connection: sqlite3.Connection) -> bool:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    return result is not None and str(result[0]) == "ok"


def _setting_provenance_value(raw_value: object) -> int | None:
    try:
        value = json.loads(str(raw_value))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _setting_provenance_time(raw_value: object) -> datetime | None:
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_explicit_setting_provenance(
    connection: sqlite3.Connection, key: str
) -> tuple[int, str] | None:
    events: list[tuple[datetime, int, str]] = []
    ambiguous = False
    history_rows = connection.execute(
        """
        SELECT id,changed_at,changed_by,new_value_summary
        FROM setting_history
        WHERE key=?
        ORDER BY id
        """,
        (key,),
    ).fetchall()
    for row in history_rows:
        value = _setting_provenance_value(row["new_value_summary"])
        timestamp = _setting_provenance_time(row["changed_at"])
        actor = str(row["changed_by"] or "").strip()
        if value is None or timestamp is None or not actor:
            ambiguous = True
            continue
        events.append((timestamp, value, actor))

    snapshot_rows = connection.execute(
        """
        SELECT s.created_at,s.actor_id,i.new_value_json
        FROM settings_snapshot_items AS i
        JOIN settings_snapshots AS s ON s.id=i.snapshot_id
        WHERE i.key=?
        ORDER BY s.created_at,s.id
        """,
        (key,),
    ).fetchall()
    for row in snapshot_rows:
        value = _setting_provenance_value(row["new_value_json"])
        timestamp = _setting_provenance_time(row["created_at"])
        actor = str(row["actor_id"] or "").strip()
        if value is None or timestamp is None or not actor:
            ambiguous = True
            continue
        events.append((timestamp, value, actor))

    if ambiguous or not events:
        return None

    unique_events = sorted(set(events))
    for event in unique_events:
        same_time = [candidate for candidate in unique_events if candidate[0] == event[0]]
        if {candidate[1:] for candidate in same_time} != {event[1:]}:
            return None
    latest = unique_events[-1]
    return latest[1], latest[2]


def _apply_migration_43_data_fixes(connection: sqlite3.Connection) -> None:
    key = "system.diagnostics_cache_seconds"
    setting = connection.execute(
        "SELECT value_json,updated_by FROM settings WHERE key=?",
        (key,),
    ).fetchone()
    if setting is None:
        return
    if _setting_provenance_value(setting["value_json"]) != 21600:
        return
    if str(setting["updated_by"] or "").strip():
        return
    provenance = _latest_explicit_setting_provenance(connection, key)
    if provenance is None or provenance[0] != 300:
        return
    connection.execute(
        """
        UPDATE settings
        SET value_json='300',value_type='integer',updated_by=?,updated_at=datetime('now')
        WHERE key=? AND value_json='21600' AND (updated_by IS NULL OR updated_by='')
        """,
        (provenance[1], key),
    )


def _apply_migration_48_data_fixes(connection: sqlite3.Connection) -> None:
    """Make unknown synchronous costs nullable without losing SQLite contracts.

    SQLite cannot drop a column's NOT NULL constraint in place.  Rebuild the
    table inside the migration transaction, carry every current row and
    sequence value across, and restore all user-visible indexes/triggers after
    the table is renamed.  The explicit schema below mirrors the cumulative
    api_usage schema produced by migrations 1, 26, 32, and 33.
    """

    indexes = [
        str(row["sql"])
        for row in connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type='index' AND tbl_name='api_usage' AND sql IS NOT NULL
            ORDER BY name
            """
        ).fetchall()
    ]
    triggers = [
        str(row["sql"])
        for row in connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type='trigger' AND tbl_name='api_usage' AND sql IS NOT NULL
            ORDER BY name
            """
        ).fetchall()
    ]
    sequence = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='api_usage'"
    ).fetchone()

    connection.execute(
        """
        CREATE TABLE api_usage_nullable_estimated_cost (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
            photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
            request_type TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cached_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost REAL DEFAULT 0,
            actual_cost REAL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            latency_ms INTEGER,
            status TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            batch_id TEXT REFERENCES analysis_batches(id) ON DELETE SET NULL,
            batch_item_id TEXT REFERENCES analysis_batch_items(id) ON DELETE SET NULL,
            processing_mode TEXT NOT NULL DEFAULT 'sync' CHECK(processing_mode IN ('sync','batch')),
            request_id TEXT,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reasoning_tokens >= 0),
            cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_write_tokens >= 0),
            cost_source TEXT NOT NULL DEFAULT 'unknown' CHECK(cost_source IN ('provider_reported','estimated','unknown')),
            prompt_chars INTEGER NOT NULL DEFAULT 0 CHECK(prompt_chars >= 0),
            schema_chars INTEGER NOT NULL DEFAULT 0 CHECK(schema_chars >= 0),
            request_body_bytes INTEGER NOT NULL DEFAULT 0 CHECK(request_body_bytes >= 0),
            image_bytes INTEGER NOT NULL DEFAULT 0 CHECK(image_bytes >= 0),
            provider_id TEXT REFERENCES providers(id) ON DELETE SET NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO api_usage_nullable_estimated_cost(
            id,provider,model,job_id,photo_id,request_type,input_tokens,output_tokens,
            cached_tokens,estimated_cost,actual_cost,started_at,completed_at,latency_ms,status,
            retry_count,error_code,batch_id,batch_item_id,processing_mode,request_id,
            reasoning_tokens,cache_write_tokens,cost_source,prompt_chars,schema_chars,
            request_body_bytes,image_bytes,provider_id
        )
        SELECT
            id,provider,model,job_id,photo_id,request_type,input_tokens,output_tokens,
            cached_tokens,estimated_cost,actual_cost,started_at,completed_at,latency_ms,status,
            retry_count,error_code,batch_id,batch_item_id,processing_mode,request_id,
            reasoning_tokens,cache_write_tokens,cost_source,prompt_chars,schema_chars,
            request_body_bytes,image_bytes,provider_id
        FROM api_usage
        """
    )
    connection.execute("DROP TABLE api_usage")
    connection.execute(
        "ALTER TABLE api_usage_nullable_estimated_cost RENAME TO api_usage"
    )
    if sequence is not None:
        connection.execute(
            "UPDATE sqlite_sequence SET seq=? WHERE name='api_usage'",
            (int(sequence[0]),),
        )
    for statement in indexes + triggers:
        connection.execute(statement)


def _traditional_json_text(value: object) -> str | None:
    """Return converted JSON text only when the payload actually changes."""

    if value is None:
        return None
    source = str(value)
    try:
        parsed = json.loads(source)
    except (TypeError, ValueError):
        return None

    from inktime.app.domain.analysis.traditional_chinese import to_taiwan_traditional

    converted = to_taiwan_traditional(parsed)
    if converted == parsed:
        return None
    return json.dumps(converted, ensure_ascii=False, separators=(",", ":"))


def _apply_migration_53_data_fixes(connection: sqlite3.Connection) -> None:
    """Backfill user-visible AI output without rewriting raw Provider evidence."""

    from inktime.app.domain.analysis.traditional_chinese import to_taiwan_traditional

    rows = connection.execute(
        "SELECT id,caption,side_caption,reason,types_json,raw_json,semantic_json FROM photo_analysis"
    ).fetchall()
    for row in rows:
        updates: dict[str, object] = {}
        for column in ("caption", "side_caption", "reason"):
            source = row[column]
            if source is None:
                continue
            converted = to_taiwan_traditional(str(source))
            if converted != source:
                updates[column] = converted
        for column in ("types_json", "raw_json", "semantic_json"):
            converted_json = _traditional_json_text(row[column])
            if converted_json is not None:
                updates[column] = converted_json
        if updates:
            assignments = ",".join(f"{column}=?" for column in updates)
            connection.execute(
                f"UPDATE photo_analysis SET {assignments} WHERE id=?",  # noqa: S608
                (*updates.values(), row["id"]),
            )

    for table, id_column, json_columns in (
        ("job_items", "id", ("result_json",)),
        ("ai_analysis_cache", "rowid", ("result_json",)),
        ("ai_trace_runs", "id", ("final_result_json",)),
        ("ai_trace_attempts", "id", ("response_parsed_json",)),
    ):
        selected = ",".join((id_column, *json_columns))
        rows = connection.execute(f"SELECT {selected} FROM {table}").fetchall()  # noqa: S608
        for row in rows:
            updates = {}
            for column in json_columns:
                converted_json = _traditional_json_text(row[column])
                if converted_json is not None:
                    updates[column] = converted_json
            if updates:
                assignments = ",".join(f"{column}=?" for column in updates)
                connection.execute(
                    f"UPDATE {table} SET {assignments} WHERE {id_column}=?",  # noqa: S608
                    (*updates.values(), row[id_column]),
                )


def _applied_versions(database: Database) -> set[int]:
    with database.session() as connection:
        if not _table_exists(connection, "schema_migrations"):
            return set()
        return {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}


def _assert_no_unfinished_migration(database: Database) -> None:
    unfinished = None
    try:
        with database.transaction(operation="migration_recovery") as connection:
            if not _table_exists(connection, "migration_history"):
                return
            unfinished = connection.execute(
                """
                SELECT id,schema_version,migration_name,backup_path
                FROM migration_history
                WHERE migration_status='running'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if unfinished is None:
                return
            migration = next(
                (
                    candidate
                    for candidate in MIGRATIONS
                    if candidate.version == int(unfinished["schema_version"])
                ),
                None,
            )
            committed = connection.execute(
                "SELECT name FROM schema_migrations WHERE version=?",
                (unfinished["schema_version"],),
            ).fetchone()
            if (
                migration is not None
                and str(unfinished["migration_name"]) == migration.name
                and committed is not None
                and str(committed["name"]) == migration.name
                and _migration_integrity_is_ok(connection)
            ):
                updated = connection.execute(
                    """
                    UPDATE migration_history
                    SET migration_completed_at=?,migration_status='completed',error_message=NULL
                    WHERE id=? AND migration_status='running'
                    """,
                    (_utc_now(), unfinished["id"]),
                )
                if updated.rowcount == 1:
                    return
    except MigrationError:
        raise
    except sqlite3.Error as exc:
        context = (
            "未完成 Migration"
            if unfinished is None
            else f"Migration {unfinished['schema_version']}（{unfinished['migration_name']}）"
        )
        raise MigrationError(
            f"MIGRATION-002 無法安全復原{context}；SQLite recovery transaction 失敗"
        ) from exc
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
    migration_check_started = time.monotonic()
    log_event(
        LOGGER,
        logging.DEBUG,
        "Migration check started",
        event="migration_check_started",
        phase="preflight",
    )
    had_database = database.path.exists() and database.path.stat().st_size > 0
    database.path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{database.path}.migration.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        log_event(
            LOGGER,
            logging.DEBUG,
            "Migration lock acquired",
            event="migration_lock_acquired",
            phase="preflight",
        )
        _assert_no_unfinished_migration(database)
        applied_versions = _applied_versions(database)
        known_versions = {migration.version for migration in MIGRATIONS}
        unknown_versions = applied_versions - known_versions
        if unknown_versions:
            newest = max(unknown_versions)
            log_event(
                LOGGER,
                logging.CRITICAL,
                "Database schema is newer than this InkTime build",
                event="migration_unknown_schema",
                error_code="MIGRATION-003",
                phase="preflight",
                details={"schema_version": newest},
            )
            raise MigrationError(
                f"MIGRATION-003 資料庫 Schema Version {newest} 高於本程式可支援版本；停止啟動以避免降級寫入"
            )
        has_pending_migrations = any(migration.version not in applied_versions for migration in MIGRATIONS)
        if not has_pending_migrations:
            log_event(
                LOGGER,
                logging.DEBUG,
                "No database migration is required",
                event="migration_noop",
                phase="preflight",
                duration_ms=int((time.monotonic() - migration_check_started) * 1000),
                details={"schema_version": max(applied_versions, default=0)},
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
            started_perf = time.monotonic()
            log_event(
                LOGGER,
                logging.INFO,
                "Database migration started",
                event="migration_started",
                phase="apply",
                details={
                    "schema_version": migration.version,
                    "migration_name": migration.name,
                },
            )
            history_id = _start_history(database, migration, backup_path, started_at=started_at)
            history_completed_in_transaction = False
            try:
                with database.transaction() as connection:
                    for statement in migration.statements:
                        connection.execute(statement)
                    if migration.version == 33:
                        _apply_migration_33_data_fixes(connection)
                    if migration.version == 43:
                        _apply_migration_43_data_fixes(connection)
                    if migration.version == 48:
                        _apply_migration_48_data_fixes(connection)
                    if migration.version == 53:
                        _apply_migration_53_data_fixes(connection)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, _utc_now()),
                    )
                    if history_id is None and _table_exists(connection, "migration_history"):
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
                    log_event(
                        LOGGER,
                        logging.DEBUG,
                        "Migration integrity check completed",
                        event="migration_integrity_check_completed",
                        phase="integrity",
                        details={"schema_version": migration.version, "result": "ok"},
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
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "Database migration rolled back",
                    event="migration_rolled_back",
                    error_code="MIGRATION-001",
                    phase="rollback",
                    duration_ms=int((time.monotonic() - started_perf) * 1000),
                    failure_class=type(exc).__name__,
                    retryable=False,
                    details={"schema_version": migration.version},
                    exc_info=(type(exc), exc, exc.__traceback__),
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
                    log_event(
                        LOGGER,
                        logging.CRITICAL,
                        "Migration history could not be committed safely",
                        event="migration_history_failed",
                        error_code="MIGRATION-004",
                        phase="history",
                        failure_class=type(exc).__name__,
                        retryable=False,
                        details={"schema_version": migration.version},
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    raise MigrationError(
                        "MIGRATION-004 Schema 已提交但 Migration 歷史無法完成；"
                        "平台必須停止，請由升級前備份回復，不得繼續寫入"
                    ) from exc
            applied.append(migration.version)
            applied_versions.add(migration.version)
            log_event(
                LOGGER,
                logging.INFO,
                "Database migration completed",
                event="migration_completed",
                phase="apply",
                duration_ms=int((time.monotonic() - started_perf) * 1000),
                details={
                    "schema_version": migration.version,
                    "migration_name": migration.name,
                },
            )
        if 54 in applied or 56 in applied:
            # Migrations 54 and 56 change which historical rows are eligible for the
            # semantic population.  Clear an already-loaded process-local
            # distribution before any caller can reuse it.
            from inktime.app.repositories.photos import invalidate_score_population_cache

            invalidate_score_population_cache()
        return applied


def backfill_photo_capture_dates(database: Database, *, batch_size: int = 500) -> dict[str, int]:
    """Materialize captured date fields in bounded, restart-safe keyset batches."""

    from inktime.app.domain.photos.dates import materialized_capture_fields

    size = max(1, min(int(batch_size), 1_000))
    counts = {"processed": 0, "valid": 0, "invalid": 0, "missing": 0}
    lock = database.try_acquire_operation_lock("capture-date-backfill")
    if lock is None:
        return counts
    try:
        cursor_id = ""
        while True:
            with database.session() as connection:
                rows = connection.execute(
                    "SELECT id,captured_at FROM photos "
                    "WHERE capture_date_status='pending' AND id>? ORDER BY id LIMIT ?",
                    (cursor_id, size),
                ).fetchall()
            if not rows:
                break
            updates: list[tuple[str | None, str | None, str, str]] = []
            for row in rows:
                captured_date, month_day, status = materialized_capture_fields(row["captured_at"])
                updates.append((captured_date, month_day, status, str(row["id"])))
                counts[status] += 1
            with database.transaction() as connection:
                connection.executemany(
                    "UPDATE photos SET captured_date=?,captured_month_day=?,capture_date_status=? "
                    "WHERE id=? AND capture_date_status='pending'",
                    updates,
                )
            counts["processed"] += len(rows)
            cursor_id = str(rows[-1]["id"])
    finally:
        lock.close()
    return counts
