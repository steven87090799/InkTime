from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import inktime.app.db.migrations as migrations_module
from inktime.app.db import Database, MigrationError, migrate
from inktime.app.db.migrations import Migration, MIGRATIONS
from inktime.app.repositories.analysis_batches import TERMINAL_BATCH_STATUSES
from inktime.app.repositories.resilience import ResilienceRepository


CURRENT_SCHEMA_VERSION = max(migration.version for migration in MIGRATIONS)


def _run_capture_date_backfill(database_path: str, start, results) -> None:
    if not start.wait(10):
        results.put({"error": "start timeout"})
        return
    try:
        results.put(
            migrations_module.backfill_photo_capture_dates(Database(Path(database_path)), batch_size=500)
        )
    except Exception as exc:
        results.put({"error": type(exc).__name__})


def test_fresh_database_is_migrated(tmp_path):
    assert CURRENT_SCHEMA_VERSION == 53
    database = Database(tmp_path / "inktime.db")
    assert migrate(database) == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    assert database.integrity_check() == "ok"
    with database.session() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        history = connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT schema_version) FROM migration_history WHERE migration_status='completed'"
        ).fetchone()
    assert {
        "photos",
        "jobs",
        "job_items",
        "api_usage",
        "users",
        "devices",
        "device_power_samples",
        "scoring_rule_versions",
        "migration_history",
        "scan_runs",
        "scan_errors",
        "scan_missing_candidates",
        "ai_analysis_cache",
        "settings_snapshots",
        "settings_snapshot_items",
        "analysis_batches",
        "analysis_batch_items",
        "idempotency_requests",
        "ai_trace_runs",
        "ai_trace_attempts",
    } <= tables
    assert tuple(history) == (CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)
    with database.session() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
        queue_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(device_content_queues)").fetchall()
        }
        device_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(devices)").fetchall()
        }
        job_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        provider_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(providers)").fetchall()
        }
        pairing_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'device_pairing_%'"
            ).fetchall()
        }
        pairing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(device_pairing_requests)").fetchall()
        }
        api_usage_policy = connection.execute(
            "SELECT enabled,retention_days,minimum_items_to_keep,cleanup_batch_size,dry_run "
            "FROM data_retention_policies WHERE data_type='api_usage'"
        ).fetchone()
        retention_dry_run_defaults = {
            str(row["data_type"]): int(row["dry_run"])
            for row in connection.execute(
                "SELECT data_type,dry_run FROM data_retention_policies ORDER BY data_type"
            ).fetchall()
        }
    assert {"normalized_username", "session_version", "disabled_at"} <= columns
    assert "request_fingerprint" in job_columns
    assert "model" in provider_columns
    assert {"current_displayed_at", "last_known_good_displayed_at"} <= queue_columns
    assert {
        "auth_mode",
        "pairing_state",
        "credential_version",
        "device_secret_hash",
        "previous_device_secret_hash",
        "previous_credential_expires_at",
        "repair_allowed_until",
        "offline_schedule_max_slots",
        "offline_schedule_capability_state",
        "next_offline_prepare_at",
        "last_status_sequence",
    } <= device_columns
    assert {"device_pairing_requests", "device_pairing_rate_limits"} <= pairing_tables
    assert {
        "pairing_nonce_hash",
        "pairing_code_hash",
        "config_json",
        "credential_envelope_ciphertext",
        "credential_envelope_expires_at",
        "confirmed_at",
    } <= pairing_columns
    assert "pairing_code_ciphertext" not in pairing_columns
    assert tuple(api_usage_policy) == (1, 400, 0, 200, 0)
    assert retention_dry_run_defaults == {
        "ai_trace": 0,
        "api_usage": 0,
        "decision_candidate": 1,
        "decision_trace": 1,
        "device_event": 1,
        "job_log": 1,
        "queue_event": 1,
        "shadow_preview": 1,
    }
    with database.session() as connection:
        idempotency_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(idempotency_requests)").fetchall()
        }
    assert {
        "scope_key",
        "request_fingerprint",
        "status",
        "request_snapshot_json",
        "response_json",
        "reservation_token",
        "reservation_expires_at",
        "created_at",
        "updated_at",
    } <= idempotency_columns
    with database.session() as connection:
        batch_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(analysis_batches)").fetchall()
        }
        item_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(analysis_batch_items)").fetchall()
        }
        usage_columns = {row["name"] for row in connection.execute("PRAGMA table_info(api_usage)").fetchall()}
    assert {
        "remote_batch_id",
        "output_file_id",
        "cleanup_completed_at",
        "peak_rss_bytes",
        "upload_attempt_id",
        "submission_attempt_id",
        "side_effect_version",
        "side_effect_lease_until",
        "side_effect_owner",
        "input_file_bytes",
        "input_file_deleted",
        "cleanup_final_action",
        "input_file_delete_unknown",
        "output_file_delete_unknown",
        "error_file_delete_unknown",
        "cleanup_error_code",
        "cleanup_error_message",
        "reconciliation_error_code",
        "reconciliation_error_message",
        "provider_config_revision",
        "provider_base_url_fingerprint",
        "provider_project_id",
        "provider_account_fingerprint",
    } <= batch_columns
    assert {"custom_id", "vision_request_fingerprint", "raw_response_json", "imported_at"} <= item_columns
    assert {"batch_id", "batch_item_id", "processing_mode", "reasoning_tokens"} <= usage_columns
    with database.session() as connection:
        indexes = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%batch%'"
            ).fetchall()
        }
    assert "idx_analysis_batches_remote_id" in indexes
    assert "submission_unknown" not in TERMINAL_BATCH_STATUSES
    assert "upload_unknown" not in TERMINAL_BATCH_STATUSES
    with database.session() as connection:
        index_names = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name IN ('idx_batch_items_active_job_item','idx_batch_items_active_content_request','idx_analysis_batches_remote_id')"
            ).fetchall()
        }
    assert index_names == {
        "idx_batch_items_active_job_item",
        "idx_batch_items_active_content_request",
        "idx_analysis_batches_remote_id",
    }
    with database.session() as connection:
        cleanup_index_names = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IN ('idx_data_cleanup_runs_completed','idx_data_cleanup_items_cleanup_run')"
            ).fetchall()
        }
    assert cleanup_index_names == {
        "idx_data_cleanup_runs_completed",
        "idx_data_cleanup_items_cleanup_run",
    }


def test_migration_45_adds_api_usage_policy_idempotently_without_overwriting_operator_values(
    monkeypatch, tmp_path
):
    database = Database(tmp_path / "migration-45-retention.db")
    previous_migrations = migrations_module.MIGRATIONS
    before_45 = tuple(migration for migration in previous_migrations if migration.version < 45)
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", before_45)
    assert migrate(database) == list(range(1, 45))
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO data_retention_policies(data_type,enabled,retention_days,minimum_items_to_keep,cleanup_batch_size,dry_run,updated_at) "
            "VALUES ('api_usage',1,777,3,17,0,datetime('now'))"
        )
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", previous_migrations)
    assert migrate(database) == list(range(45, CURRENT_SCHEMA_VERSION + 1))
    with database.session() as connection:
        policy = connection.execute(
            "SELECT enabled,retention_days,minimum_items_to_keep,cleanup_batch_size,dry_run "
            "FROM data_retention_policies WHERE data_type='api_usage'"
        ).fetchone()
    assert tuple(policy) == (1, 777, 3, 17, 0)
    assert migrate(database) == []


def test_migration_49_enables_only_untouched_api_usage_default(monkeypatch, tmp_path):
    all_migrations = migrations_module.MIGRATIONS
    before_49 = tuple(migration for migration in all_migrations if migration.version < 49)
    untouched = Database(tmp_path / "migration-49-untouched.db")
    administrator_dry_run = Database(tmp_path / "migration-49-administrator.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", before_49)
    assert migrate(untouched) == list(range(1, 49))
    assert migrate(administrator_dry_run) == list(range(1, 49))
    ResilienceRepository(administrator_dry_run).update_retention(
        "api_usage", {"dry_run": True}
    )

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", all_migrations)
    assert migrate(untouched) == list(range(49, CURRENT_SCHEMA_VERSION + 1))
    assert migrate(administrator_dry_run) == list(range(49, CURRENT_SCHEMA_VERSION + 1))
    with untouched.session() as connection:
        untouched_policy = connection.execute(
            "SELECT enabled,retention_days,cleanup_batch_size,dry_run "
            "FROM data_retention_policies WHERE data_type='api_usage'"
        ).fetchone()
    with administrator_dry_run.session() as connection:
        administrator_policy = connection.execute(
            "SELECT enabled,retention_days,cleanup_batch_size,dry_run "
            "FROM data_retention_policies WHERE data_type='api_usage'"
        ).fetchone()
    assert tuple(untouched_policy) == (1, 400, 200, 0)
    assert tuple(administrator_policy) == (1, 400, 200, 1)
    assert migrate(untouched) == []
    assert migrate(administrator_dry_run) == []


def test_migration_50_adds_cleanup_audit_gc_indexes_idempotently(monkeypatch, tmp_path):
    database = Database(tmp_path / "migration-50-cleanup-audit-indexes.db")
    all_migrations = migrations_module.MIGRATIONS
    before_50 = tuple(migration for migration in all_migrations if migration.version < 50)
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", before_50)
    assert migrate(database) == list(range(1, 50))

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", all_migrations)
    assert migrate(database) == list(range(50, CURRENT_SCHEMA_VERSION + 1))
    with database.session() as connection:
        indexes = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IN ('idx_data_cleanup_runs_completed','idx_data_cleanup_items_cleanup_run')"
            ).fetchall()
        }
    assert indexes == {
        "idx_data_cleanup_runs_completed",
        "idx_data_cleanup_items_cleanup_run",
    }
    assert migrate(database) == []


def test_migration_53_converts_existing_ai_output_to_taiwan_traditional(monkeypatch, tmp_path):
    database = Database(tmp_path / "migration-53-traditional.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:52])
    assert migrate(database) == list(range(1, 53))

    simplified = {
        "caption": "他们在复古小镇看着风景。",
        "side_caption": "他们看着风景",
        "reason": "画面里的年轻人关系亲近",
        "details": {"scene": "复古小镇街头"},
    }
    simplified_json = json.dumps(simplified, ensure_ascii=False)
    with database.session() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) "
            "VALUES ('lib','測試','/photos',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at) "
            "VALUES ('photo','lib','photo.jpg','analyzed',datetime('now'),datetime('now'))"
        )
        connection.execute(
            """
            INSERT INTO photo_analysis(
                photo_id,schema_version,stage,caption,types_json,side_caption,reason,
                raw_json,semantic_json,created_at
            ) VALUES ('photo',3,'single',?,'["人物"]',?,?,?,?,datetime('now'))
            """,
            (
                simplified["caption"],
                simplified["side_caption"],
                simplified["reason"],
                simplified_json,
                json.dumps({"values": simplified["details"]}, ensure_ascii=False),
            ),
        )
        connection.execute(
            """
            INSERT INTO ai_analysis_cache(
                content_sha256,provider,model_name,prompt_version,schema_version,
                schema_kind,result_json,raw_json,created_at
            ) VALUES ('sha','provider','model','prompt',3,'full',?,?,datetime('now'))
            """,
            (simplified_json, simplified_json),
        )
        connection.execute(
            """
            INSERT INTO ai_trace_runs(
                trace_id,photo_id,stage,status,started_at,final_result_json,created_at
            ) VALUES ('trace','photo','single','SUCCESS',datetime('now'),?,datetime('now'))
            """,
            (simplified_json,),
        )
        connection.execute(
            """
            INSERT INTO ai_trace_attempts(
                trace_id,attempt_number,attempt_kind,provider,requested_model,status,
                response_raw_sanitized,response_parsed_json,created_at
            ) VALUES ('trace',1,'vision','provider','model','SUCCESS',?,?,datetime('now'))
            """,
            (simplified_json, simplified_json),
        )

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    assert migrate(database) == [53]
    with database.session() as connection:
        analysis = connection.execute(
            "SELECT caption,side_caption,reason,raw_json,semantic_json FROM photo_analysis"
        ).fetchone()
        cached = connection.execute("SELECT result_json,raw_json FROM ai_analysis_cache").fetchone()
        trace = connection.execute("SELECT final_result_json FROM ai_trace_runs").fetchone()
        attempt = connection.execute(
            "SELECT response_raw_sanitized,response_parsed_json FROM ai_trace_attempts"
        ).fetchone()

    assert tuple(analysis[:3]) == (
        "他們在復古小鎮看著風景。",
        "他們看著風景",
        "畫面裡的年輕人關係親近",
    )
    assert json.loads(analysis["raw_json"])["details"]["scene"] == "復古小鎮街頭"
    assert json.loads(analysis["semantic_json"])["values"]["scene"] == "復古小鎮街頭"
    assert json.loads(cached["result_json"])["caption"] == "他們在復古小鎮看著風景。"
    assert cached["raw_json"] == simplified_json
    assert json.loads(trace["final_result_json"])["side_caption"] == "他們看著風景"
    assert json.loads(attempt["response_parsed_json"])["reason"] == "畫面裡的年輕人關係親近"
    assert attempt["response_raw_sanitized"] == simplified_json


def test_migration_46_idempotency_ledger_is_upgrade_safe(monkeypatch, tmp_path):
    database = Database(tmp_path / "migration-46-ledger.db")
    all_migrations = migrations_module.MIGRATIONS
    before_46 = tuple(migration for migration in all_migrations if migration.version < 46)
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", before_46)
    assert migrate(database) == list(range(1, 46))
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", all_migrations)
    assert migrate(database) == list(range(46, CURRENT_SCHEMA_VERSION + 1))
    with database.session() as connection:
        connection.execute(
            "INSERT INTO idempotency_requests(scope_key,request_fingerprint,status,request_snapshot_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("scope", "fingerprint", "in_progress", "{}", "now", "now"),
        )
    assert migrate(database) == []


def test_migration_48_makes_unknown_cost_nullable_and_preserves_api_usage_contract(
    monkeypatch, tmp_path
):
    database = Database(tmp_path / "migration-48-nullable-cost.db")
    all_migrations = migrations_module.MIGRATIONS
    before_48 = tuple(migration for migration in all_migrations if migration.version < 48)
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", before_48)
    assert migrate(database) == list(range(1, 48))
    with database.transaction() as connection:
        connection.execute(
            "CREATE INDEX idx_api_usage_migration_custom ON api_usage(provider,status)"
        )
        connection.execute(
            """
            INSERT INTO api_usage(
                id,provider,model,request_type,input_tokens,estimated_cost,actual_cost,
                started_at,status,cost_source
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (17, "legacy", "model", "analysis", 3, 0.125, None, "now", "completed", "estimated"),
        )
        connection.execute(
            """
            INSERT INTO api_usage(
                id,provider,model,request_type,started_at,status
            ) VALUES (900,"sequence","model","analysis","now","completed")
            """
        )
        connection.execute("DELETE FROM api_usage WHERE id=900")
        foreign_keys_before = {
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_list(api_usage)").fetchall()
        }

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", all_migrations)
    assert migrate(database) == list(range(48, CURRENT_SCHEMA_VERSION + 1))
    with database.transaction() as connection:
        estimated_column = next(
            row for row in connection.execute("PRAGMA table_info(api_usage)").fetchall()
            if row["name"] == "estimated_cost"
        )
        foreign_keys_after = {
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_list(api_usage)").fetchall()
        }
        index_names = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='api_usage'"
            ).fetchall()
        }
        legacy = connection.execute(
            "SELECT id,provider,input_tokens,estimated_cost,actual_cost,cost_source FROM api_usage WHERE id=17"
        ).fetchone()
        connection.execute(
            """
            INSERT INTO api_usage(provider,model,request_type,estimated_cost,actual_cost,started_at,status)
            VALUES (?,?,?,?,?,?,?)
            """,
            ("unknown", "model", "analysis", None, None, "now", "failed"),
        )
        auto_id = connection.execute(
            "SELECT id FROM api_usage WHERE provider='unknown'"
        ).fetchone()[0]

    assert estimated_column[3] == 0
    assert estimated_column[4] == "0"
    assert foreign_keys_after == foreign_keys_before
    assert "idx_api_usage_migration_custom" in index_names
    assert "idx_api_usage_batch_item_once" in index_names
    assert tuple(legacy) == (17, "legacy", 3, 0.125, None, "estimated")
    assert auto_id > 900
    assert migrate(database) == []


def test_migration_39_quarantines_legacy_ambiguous_offline_slot_rows(monkeypatch, tmp_path):
    database = Database(tmp_path / "migration-39-legacy-slots.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:38])
    assert migrate(database) == list(range(1, 39))
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO devices(
                id,name,token_hash,enabled,timezone,schedule,delivery_mode,
                offline_prefetch_allowed,schedule_times_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-ambiguous",
                "Legacy ambiguous",
                "token-ambiguous",
                1,
                "Asia/Taipei",
                "08:00",
                "inktime_offline_schedule",
                1,
                json.dumps([f"{hour:02d}:00" for hour in range(8, 21)]),
                "2026-08-08T00:00:00+00:00",
                "2026-08-08T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO devices(
                id,name,token_hash,enabled,timezone,schedule,delivery_mode,
                offline_prefetch_allowed,schedule_times_json,offline_schedule_json,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-malformed-ambiguous",
                "Legacy malformed ambiguous",
                "token-malformed",
                1,
                "Asia/Taipei",
                "08:00",
                "inktime_offline_schedule",
                1,
                '["08:00",',
                '["08:00"]',
                "2026-08-08T00:00:00+00:00",
                "2026-08-08T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO devices(
                id,name,token_hash,enabled,timezone,schedule,delivery_mode,
                offline_prefetch_allowed,schedule_times_json,offline_schedule_json,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-non-array-ambiguous",
                "Legacy non-array ambiguous",
                "token-non-array",
                1,
                "Asia/Taipei",
                "08:00",
                "inktime_offline_schedule",
                1,
                '{"legacy":"08:00"}',
                '"08:00"',
                "2026-08-08T00:00:00+00:00",
                "2026-08-08T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO devices(
                id,name,token_hash,enabled,timezone,schedule,delivery_mode,
                offline_prefetch_allowed,schedule_times_json,offline_schedule_json,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-fallback-ambiguous",
                "Legacy fallback ambiguous",
                "token-fallback",
                1,
                "Asia/Taipei",
                "08:00",
                "inktime_offline_schedule",
                1,
                "[]",
                json.dumps([f"{hour:02d}:00" for hour in range(8, 21)]),
                "2026-08-08T00:00:00+00:00",
                "2026-08-08T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO devices(
                id,name,token_hash,enabled,timezone,schedule,delivery_mode,
                offline_prefetch_allowed,schedule_times_json,offline_schedule_json,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-mirror-ambiguous",
                "Legacy mirror ambiguous",
                "token-mirror",
                1,
                "Asia/Taipei",
                "08:00",
                "inktime_offline_schedule",
                1,
                json.dumps([f"{hour:02d}:00" for hour in range(8, 20)]),
                json.dumps([f"{hour:02d}:00" for hour in range(8, 21)]),
                "2026-08-08T00:00:00+00:00",
                "2026-08-08T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO devices(
                id,name,token_hash,enabled,timezone,schedule,delivery_mode,
                offline_prefetch_allowed,schedule_times_json,offline_schedule_max_slots,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-safe",
                "Legacy safe",
                "token-safe",
                1,
                "Asia/Taipei",
                "08:00",
                "inktime_offline_schedule",
                1,
                json.dumps([f"{hour:02d}:00" for hour in range(8, 20)]),
                12,
                "2026-08-08T00:00:00+00:00",
                "2026-08-08T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO devices(
                id,name,token_hash,enabled,timezone,schedule,delivery_mode,
                offline_prefetch_allowed,schedule_times_json,offline_schedule_max_slots,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-confirmed-24",
                "Legacy confirmed 24",
                "token-confirmed-24",
                1,
                "Asia/Taipei",
                "08:00",
                "inktime_offline_schedule",
                1,
                json.dumps([f"{hour:02d}:00" for hour in range(0, 24)]),
                24,
                "2026-08-08T00:00:00+00:00",
                "2026-08-08T00:00:00+00:00",
            ),
        )

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:39])
    assert migrate(database) == [39]
    with database.session() as connection:
        row = connection.execute(
            "SELECT offline_schedule_max_slots,offline_schedule_capability_state,next_offline_prepare_at,schedule_times_json "
            "FROM devices WHERE id='legacy-ambiguous'"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="DEVICE-008"):
            connection.execute(
                "UPDATE devices SET offline_schedule_max_slots=13 WHERE id='legacy-ambiguous'"
            )
        states = connection.execute(
            "SELECT id,offline_schedule_max_slots,offline_schedule_capability_state,next_offline_prepare_at FROM devices WHERE id IN ('legacy-safe','legacy-confirmed-24') ORDER BY id"
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="DEVICE-008"):
            connection.execute(
                "UPDATE devices SET offline_schedule_capability_state='confirmed_24' WHERE id='legacy-safe'"
            )
    assert (
        row["offline_schedule_max_slots"],
        row["offline_schedule_capability_state"],
        row["next_offline_prepare_at"],
    ) == (12, "legacy_ambiguous", None)
    assert json.loads(str(row[3])) == [f"{hour:02d}:00" for hour in range(8, 21)]
    assert [tuple(item) for item in states] == [
        ("legacy-confirmed-24", 24, "confirmed_24", "1970-01-01T00:00:00+00:00"),
        ("legacy-safe", 12, "unknown_12", "1970-01-01T00:00:00+00:00"),
    ]
    with database.session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_events WHERE event='offline_schedule_capability_quarantined' AND error_code='DEVICE-008'"
        ).fetchone()[0] == 5
    with database.session() as connection:
        ambiguous_rows = connection.execute(
            """
            SELECT id,offline_schedule_max_slots,offline_schedule_capability_state,
                   next_offline_prepare_at,schedule_times_json,offline_schedule_json
            FROM devices
            WHERE id IN (
                'legacy-fallback-ambiguous',
                'legacy-malformed-ambiguous',
                'legacy-mirror-ambiguous',
                'legacy-non-array-ambiguous'
            )
            ORDER BY id
            """
        ).fetchall()
    assert [
        (
            row["id"],
            row["offline_schedule_max_slots"],
            row["offline_schedule_capability_state"],
            row["next_offline_prepare_at"],
            row["schedule_times_json"],
            row["offline_schedule_json"],
        )
        for row in ambiguous_rows
    ] == [
        (
            "legacy-fallback-ambiguous",
            12,
            "legacy_ambiguous",
            None,
            "[]",
            json.dumps([f"{hour:02d}:00" for hour in range(8, 21)]),
        ),
        (
            "legacy-malformed-ambiguous",
            12,
            "legacy_ambiguous",
            None,
            '["08:00",',
            '["08:00"]',
        ),
        (
            "legacy-mirror-ambiguous",
            12,
            "legacy_ambiguous",
            None,
            json.dumps([f"{hour:02d}:00" for hour in range(8, 20)]),
            json.dumps([f"{hour:02d}:00" for hour in range(8, 21)]),
        ),
        (
            "legacy-non-array-ambiguous",
            12,
            "legacy_ambiguous",
            None,
            '{"legacy":"08:00"}',
            '"08:00"',
        ),
    ]
    assert migrate(database) == []


def test_migration_44_backfills_missing_device_008_warning_idempotently(monkeypatch, tmp_path):
    database = Database(tmp_path / "migration-44-missing-device-warning.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:39])
    assert migrate(database) == list(range(1, 40))
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO devices(
                id,name,token_hash,enabled,timezone,schedule,delivery_mode,
                offline_prefetch_allowed,schedule_times_json,offline_schedule_json,
                offline_schedule_max_slots,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "migration-44-ambiguous",
                "Migration 44 ambiguous",
                "token-migration-44",
                1,
                "Asia/Taipei",
                "08:00",
                "inktime_offline_schedule",
                1,
                json.dumps([f"{hour:02d}:00" for hour in range(8, 21)]),
                "[]",
                12,
                "2026-08-08T00:00:00+00:00",
                "2026-08-08T00:00:00+00:00",
            ),
        )
        connection.execute(
            "UPDATE devices SET offline_schedule_capability_state='legacy_ambiguous' WHERE id=?",
            ("migration-44-ambiguous",),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM device_events WHERE device_id=? AND error_code='DEVICE-008'",
            ("migration-44-ambiguous",),
        ).fetchone()[0] == 0

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    assert migrate(database) == list(range(40, CURRENT_SCHEMA_VERSION + 1))
    with database.session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_events WHERE device_id=? AND error_code='DEVICE-008'",
            ("migration-44-ambiguous",),
        ).fetchone()[0] == 1

    with database.transaction() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=44")
    assert migrate(database) == [44]
    with database.session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_events WHERE device_id=? AND error_code='DEVICE-008'",
            ("migration-44-ambiguous",),
        ).fetchone()[0] == 1


def test_batch_unknown_states_and_reservations_are_persistent(tmp_path):
    database = Database(tmp_path / "states.db")
    migrate(database)
    with database.transaction() as connection:
        for batch_id, status in (("batch-a", "submission_unknown"), ("batch-b", "upload_unknown")):
            connection.execute(
                "INSERT INTO analysis_batches(id,model,endpoint,analysis_fingerprint,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (batch_id, "gpt-5.6-luna", "/v1/chat/completions", "fp", status, "now", "now"),
            )
        connection.execute(
            "INSERT INTO analysis_batch_items(id,batch_id,custom_id,content_sha256,analysis_fingerprint,vision_request_fingerprint,vision_input_spec_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "item-a",
                "batch-a",
                "ibt:00000000-0000-0000-0000-000000000001",
                "sha",
                "fp",
                "vfp",
                "{}",
                "submission_unknown",
                "now",
                "now",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO analysis_batch_items(id,batch_id,custom_id,content_sha256,analysis_fingerprint,vision_request_fingerprint,vision_input_spec_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "item-b",
                    "batch-b",
                    "ibt:00000000-0000-0000-0000-000000000002",
                    "sha",
                    "fp",
                    "vfp",
                    "{}",
                    "pending",
                    "now",
                    "now",
                ),
            )


def test_migration_25_to_batch_lifecycle_is_idempotent(monkeypatch, tmp_path):
    database = Database(tmp_path / "inktime.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:25])
    migrate(database)
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    assert migrate(database, tmp_path / "backups") == list(range(26, CURRENT_SCHEMA_VERSION + 1))
    assert migrate(database, tmp_path / "backups") == []
    assert database.integrity_check() == "ok"


def test_migration_32_preserves_legacy_cost_provenance_and_allows_new_reported_cost(monkeypatch, tmp_path):
    database = Database(tmp_path / "cost-provenance.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:31])
    migrate(database)
    with database.transaction() as connection:
        connection.executemany(
            "INSERT INTO api_usage(provider,model,request_type,estimated_cost,actual_cost,started_at,status) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("legacy", "model", "analysis", 0.10, 0.10, "now", "completed"),
                ("legacy", "model", "analysis", 0.00, 0.00, "now", "completed"),
                ("legacy", "model", "analysis", 0.00, None, "now", "completed"),
            ],
        )
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    assert migrate(database) == list(range(32, CURRENT_SCHEMA_VERSION + 1))
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO api_usage(provider,model,request_type,estimated_cost,actual_cost,started_at,status,cost_source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("openrouter", "model", "analysis", 0.0, 0.08, "now", "completed", "provider_reported"),
        )
        rows = connection.execute(
            "SELECT estimated_cost,actual_cost,cost_source FROM api_usage ORDER BY id"
        ).fetchall()
    assert [row["cost_source"] for row in rows] == ["estimated", "unknown", "unknown", "provider_reported"]
    assert rows[0]["actual_cost"] == 0.10
    assert rows[1]["estimated_cost"] == 0.0
    assert rows[1]["actual_cost"] == 0.0
    assert rows[1]["cost_source"] == "unknown"
    assert rows[2]["estimated_cost"] == 0.0
    assert rows[2]["actual_cost"] is None
    assert rows[3]["actual_cost"] == 0.08


def test_migration_33_backfills_provider_identity_and_keeps_billable_unknown(monkeypatch, tmp_path):
    database = Database(tmp_path / "migration-33-provider.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:32])
    migrate(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO providers(id,name,kind,base_url,supports_batch,options_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (
                "legacy-openrouter-id",
                "legacy-openrouter",
                "openai_compatible",
                "https://openrouter.ai/api/v1",
                1,
                '{"privacy":"private","route":"fallback"}',
            ),
        )
        connection.executemany(
            "INSERT INTO api_usage(provider,model,request_type,estimated_cost,actual_cost,input_tokens,started_at,status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                ("legacy-openrouter", "model", "analysis", 0.0, None, 0, "now", "completed"),
                ("legacy-openrouter", "model", "analysis", 0.0, None, 3, "now", "completed"),
            ],
        )
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    assert migrate(database) == list(range(33, CURRENT_SCHEMA_VERSION + 1))
    with database.transaction() as connection:
        provider = connection.execute(
            "SELECT id,kind,supports_batch,options_json FROM providers WHERE name=?",
            ("legacy-openrouter",),
        ).fetchone()
        rows = connection.execute(
            "SELECT provider_id,input_tokens,estimated_cost,actual_cost,cost_source "
            "FROM api_usage ORDER BY id"
        ).fetchall()
    assert provider["id"] == "legacy-openrouter-id"
    assert provider["kind"] == "openrouter"
    assert provider["supports_batch"] == 0
    assert json.loads(provider["options_json"]) == {
        "privacy": "private",
        "require_parameters": True,
        "route": "fallback",
    }
    assert rows[0]["provider_id"] == "legacy-openrouter-id"
    assert rows[0]["cost_source"] == "unknown"
    assert rows[0]["estimated_cost"] == 0.0
    assert rows[0]["actual_cost"] is None
    assert rows[1]["provider_id"] == "legacy-openrouter-id"
    assert rows[1]["cost_source"] == "unknown"


def test_migration_27_to_30_freezes_ownership_and_invalidates_legacy_ready_rows(monkeypatch, tmp_path):
    database = Database(tmp_path / "legacy-offline.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:27])
    assert migrate(database) == list(range(1, 28))
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO devices(id,name,token_hash,timezone,schedule,created_at,updated_at) "
            "VALUES ('legacy-device','Legacy PhotoPainter','legacy-token','Asia/Taipei','08:00',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO releases(id,display_type,width,height,pixel_format,manifest_json,status,created_at,created_by) "
            "VALUES ('legacy-release','photo',800,480,'4bpp','{}','published',datetime('now'),'migration-test')"
        )
        connection.execute(
            "INSERT INTO releases(id,display_type,width,height,pixel_format,manifest_json,status,created_at,created_by) "
            "VALUES ('online-release','photo',800,480,'4bpp','{}','published',datetime('now'),'migration-test')"
        )
        connection.execute(
            "INSERT INTO device_content_queues(device_id,depth,queue_version,updated_at) "
            "VALUES ('legacy-device',3,1,datetime('now'))"
        )
        connection.execute(
            "INSERT INTO device_offline_schedules(id,device_id,target_date,config_version,timezone,status,created_at,updated_at) "
            "VALUES ('legacy-schedule','legacy-device','2026-08-03',1,'Asia/Taipei','ready',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO device_content_queue_items(id,device_id,release_id,position,status,delivery_mode,offline_prefetch_allowed,offline_slot,ack_deadline,created_at,updated_at) "
            "VALUES ('legacy-item','legacy-device','legacy-release',1,'READY','offline_schedule',1,'08:00','2026-08-03T08:10:00+00:00',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO device_offline_schedule_slots(id,schedule_id,slot_index,show_at,release_id,queue_item_id,sha256,created_at) "
            "VALUES ('legacy-slot','legacy-schedule',0,'2026-08-03T08:00:00+00:00','legacy-release','legacy-item',?,datetime('now'))",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO device_content_queue_items(id,device_id,release_id,position,status,delivery_mode,created_at,updated_at) "
            "VALUES ('online-item','legacy-device','online-release',2,'READY','online_queue',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) "
            "VALUES ('legacy-library','Legacy','/photos',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at) "
            "VALUES ('legacy-photo','legacy-library','legacy.jpg','analyzed',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO photo_analysis(photo_id,schema_version,stage,raw_json,created_at) "
            "VALUES ('legacy-photo',3,'single','{}',datetime('now'))"
        )
        analysis_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO photo_reviews(photo_id,analysis_id,review_state,version,updated_at) "
            "VALUES ('legacy-photo',?,'exclude',3,datetime('now'))",
            (analysis_id,),
        )

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    assert migrate(database, tmp_path / "backups") == list(range(28, CURRENT_SCHEMA_VERSION + 1))
    with database.session() as connection:
        schedule = connection.execute(
            "SELECT status,panel_profile,rotation,snapshot_json FROM device_offline_schedules WHERE id='legacy-schedule'"
        ).fetchone()
        offline_item = connection.execute(
            "SELECT status,offline_schedule_id,terminal_ack_retention FROM device_content_queue_items WHERE id='legacy-item'"
        ).fetchone()
        online_item = connection.execute(
            "SELECT status,offline_schedule_id FROM device_content_queue_items WHERE id='online-item'"
        ).fetchone()
        decision = connection.execute(
            "SELECT analysis_id,review_state,version FROM photo_reviews WHERE photo_id='legacy-photo' AND analysis_id IS NULL"
        ).fetchone()
        cursor = connection.execute(
            "SELECT id,last_device_id FROM device_offline_prefetch_cursors"
        ).fetchone()
    assert tuple(schedule) == ("cancelled", "safe_4c", 0, "{}")
    assert tuple(offline_item) == ("CANCELLED", "legacy-schedule", None)
    assert tuple(online_item) == ("READY", None)
    assert tuple(decision) == (None, "exclude", 3)
    assert tuple(cursor) == (1, None)
    with database.session() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO device_content_queue_items(id,device_id,release_id,position,status,delivery_mode,created_at,updated_at) "
                "VALUES ('orphan-offline','legacy-device','online-release',3,'READY','offline_schedule',datetime('now'),datetime('now'))"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE device_content_queue_items SET offline_schedule_id='legacy-schedule' WHERE id='online-item'"
            )
        schedule_fk = connection.execute(
            "SELECT on_delete FROM pragma_foreign_key_list('device_content_queue_items') "
            "WHERE \"table\"='device_offline_schedules' AND \"from\"='offline_schedule_id'"
        ).fetchone()
        assert schedule_fk["on_delete"] == "RESTRICT"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM device_offline_schedules WHERE id='legacy-schedule'")


def test_migration_29_to_30_adds_pointer_times_and_mode_guards_with_backup_restart(monkeypatch, tmp_path):
    database = Database(tmp_path / "migration-30.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:29])
    assert migrate(database) == list(range(1, 30))
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO devices(id,name,token_hash,timezone,schedule,created_at,updated_at) "
            "VALUES ('mode-device','Mode Guard','mode-token','Asia/Taipei','08:00',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO releases(id,display_type,width,height,pixel_format,manifest_json,status,created_at,created_by) "
            "VALUES ('mode-release','photo',800,480,'4bpp','{}','published',datetime('now'),'migration-test')"
        )
        connection.execute(
            "INSERT INTO device_content_queues(device_id,depth,queue_version,updated_at) "
            "VALUES ('mode-device',3,0,datetime('now'))"
        )

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    backups = tmp_path / "backups"
    assert migrate(database, backups) == list(range(30, CURRENT_SCHEMA_VERSION + 1))
    backup_files = list(backups.glob("*.sqlite3"))
    assert len(backup_files) == 1
    with sqlite3.connect(backup_files[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with database.session() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(device_content_queues)").fetchall()
        }
        connection.execute(
            "UPDATE devices SET delivery_mode='inktime_offline_schedule',offline_prefetch_allowed=1 "
            "WHERE id='mode-device'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="QUEUE-005"):
            connection.execute(
                "INSERT INTO device_content_queue_items(id,device_id,release_id,position,status,delivery_mode,created_at,updated_at) "
                "VALUES ('blocked-online','mode-device','mode-release',1,'READY','online_queue',datetime('now'),datetime('now'))"
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert {"current_displayed_at", "last_known_good_displayed_at"} <= columns
    assert migrate(database, backups) == []


def test_migration_31_repairs_delivery_prefetch_and_guards_insert_update(monkeypatch, tmp_path):
    database = Database(tmp_path / "migration-31.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:30])
    assert migrate(database) == list(range(1, 31))
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO devices(id,name,token_hash,timezone,schedule,delivery_mode,offline_prefetch_allowed,created_at,updated_at) "
            "VALUES ('enhanced-legacy','Enhanced legacy','token-a','Asia/Taipei','08:00','inktime_offline_schedule',0,datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO devices(id,name,token_hash,timezone,schedule,delivery_mode,offline_prefetch_allowed,created_at,updated_at) "
            "VALUES ('online-legacy','Online legacy','token-b','Asia/Taipei','08:00','legacy_online',1,datetime('now'),datetime('now'))"
        )

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:31])
    backups = tmp_path / "backups"
    assert migrate(database, backups) == [31]
    backup_files = list(backups.glob("*.sqlite3"))
    assert len(backup_files) == 1
    with sqlite3.connect(backup_files[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with database.session() as connection:
        repaired = connection.execute(
            "SELECT id,delivery_mode,offline_prefetch_allowed FROM devices WHERE id IN ('enhanced-legacy','online-legacy') ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in repaired] == [
            ("enhanced-legacy", "inktime_offline_schedule", 1),
            ("online-legacy", "legacy_online", 0),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="DEVICE-008"):
            connection.execute(
                "INSERT INTO devices(id,name,token_hash,timezone,schedule,delivery_mode,offline_prefetch_allowed,created_at,updated_at) "
                "VALUES ('blocked-device','Blocked','token-c','Asia/Taipei','08:00','legacy_online',1,datetime('now'),datetime('now'))"
            )
        with pytest.raises(sqlite3.IntegrityError, match="DEVICE-008"):
            connection.execute(
                "UPDATE devices SET delivery_mode='inktime_offline_schedule',offline_prefetch_allowed=0 WHERE id='online-legacy'"
            )
        connection.execute(
            "UPDATE devices SET delivery_mode='inktime_offline_schedule',offline_prefetch_allowed=1 WHERE id='online-legacy'"
        )
        connection.execute(
            "UPDATE devices SET delivery_mode='stock_compat',offline_prefetch_allowed=0 WHERE id='online-legacy'"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert migrate(database, backups) == []


def test_existing_photo_scores_table_is_preserved(tmp_path):
    path = tmp_path / "photos.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE photo_scores(path TEXT PRIMARY KEY, caption TEXT)")
    connection.execute("INSERT INTO photo_scores VALUES ('/photos/a.jpg', '回憶')")
    connection.commit()
    connection.close()

    database = Database(path)
    migrate(database, tmp_path / "backups")
    with database.session() as migrated:
        assert migrated.execute("SELECT caption FROM photo_scores").fetchone()[0] == "回憶"
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
    assert migrate(database, tmp_path / "backups") == []
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1


def test_failed_migration_rolls_back(monkeypatch, tmp_path):
    broken = Migration(
        999,
        "故意失敗",
        ("CREATE TABLE must_be_rolled_back(id INTEGER)", "INVALID SQL"),
    )
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS + (broken,))
    database = Database(tmp_path / "inktime.db")
    with pytest.raises(MigrationError):
        migrate(database)
    with database.session() as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='must_be_rolled_back'"
        ).fetchone()
        recorded = connection.execute("SELECT version FROM schema_migrations WHERE version=999").fetchone()
        history = connection.execute(
            "SELECT migration_status,migration_completed_at FROM migration_history WHERE schema_version=999"
        ).fetchone()
    assert table is None
    assert recorded is None
    assert history["migration_status"] == "rolled_back"
    assert history["migration_completed_at"]


def test_history_completion_failure_keeps_running_marker_and_stops_restart(monkeypatch, tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    committed = Migration(999, "收尾失敗", ("CREATE TABLE committed_schema(id INTEGER)",))
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS + (committed,))
    original_finish = migrations_module._finish_history

    def fail_completed_history(*args, status, **kwargs):
        if status == "completed" and args[1].version == 999:
            raise OSError("forced history completion failure")
        return original_finish(*args, status=status, **kwargs)

    monkeypatch.setattr(migrations_module, "_finish_history", fail_completed_history)
    with pytest.raises(MigrationError, match="MIGRATION-004"):
        migrate(database, tmp_path / "backups")

    with database.session() as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='committed_schema'").fetchone()
        assert connection.execute("SELECT 1 FROM schema_migrations WHERE version=999").fetchone()
        assert (
            connection.execute(
                "SELECT migration_status FROM migration_history WHERE schema_version=999"
            ).fetchone()[0]
            == "running"
        )
    assert migrate(database, tmp_path / "backups") == []
    with database.session() as connection:
        recovered = connection.execute(
            "SELECT migration_status,migration_completed_at FROM migration_history WHERE schema_version=999"
        ).fetchone()
    assert recovered["migration_status"] == "completed"
    assert recovered["migration_completed_at"]


def test_known_unfinished_migration_name_mismatch_stays_fail_closed(tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    with database.session() as connection:
        connection.execute(
            """
            INSERT INTO migration_history(
                schema_version,migration_name,migration_started_at,migration_status
            ) VALUES (42,'錯誤名稱',datetime('now'),'running')
            """
        )

    with pytest.raises(MigrationError, match="MIGRATION-002"):
        migrate(database)
    with database.session() as connection:
        assert (
            connection.execute(
                "SELECT migration_status FROM migration_history WHERE migration_name='錯誤名稱'"
            ).fetchone()[0]
            == "running"
        )


def test_consistent_unfinished_migration_with_failed_integrity_check_stays_blocked(monkeypatch, tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    with database.session() as connection:
        connection.execute(
            """
            INSERT INTO migration_history(
                schema_version,migration_name,migration_started_at,migration_status
            ) VALUES (42,'保存昂貴 POST 的 Idempotency request fingerprint',datetime('now'),'running')
            """
        )
    monkeypatch.setattr(migrations_module, "_migration_integrity_is_ok", lambda _connection: False)

    with pytest.raises(MigrationError, match="MIGRATION-002"):
        migrate(database)
    with database.session() as connection:
        assert (
            connection.execute(
                "SELECT migration_status FROM migration_history WHERE migration_name='保存昂貴 POST 的 Idempotency request fingerprint' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            == "running"
        )


def test_unfinished_migration_recovery_write_failure_stays_running(tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    migration_name = "保存昂貴 POST 的 Idempotency request fingerprint"
    with database.session() as connection:
        connection.execute(
            """
            INSERT INTO migration_history(
                schema_version,migration_name,migration_started_at,migration_status
            ) VALUES (42,?,datetime('now'),'running')
            """,
            (migration_name,),
        )
        connection.execute(
            """
            CREATE TRIGGER fail_migration_recovery
            BEFORE UPDATE OF migration_status ON migration_history
            WHEN OLD.migration_status='running' AND NEW.migration_status='completed'
            BEGIN
                SELECT RAISE(ABORT,'forced recovery update failure');
            END
            """
        )

    with pytest.raises(MigrationError, match="MIGRATION-002"):
        migrate(database)
    with database.session() as connection:
        assert (
            connection.execute(
                "SELECT migration_status FROM migration_history WHERE schema_version=42 AND migration_name=? ORDER BY id DESC LIMIT 1",
                (migration_name,),
            ).fetchone()[0]
            == "running"
        )


def test_unknown_unfinished_migration_stays_fail_closed(tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    with database.session() as connection:
        connection.execute(
            """
            INSERT INTO migration_history(
                schema_version,migration_name,migration_started_at,migration_status
            ) VALUES (999,'未知 Migration',datetime('now'),'running')
            """
        )

    with pytest.raises(MigrationError, match="MIGRATION-002"):
        migrate(database)
    with database.session() as connection:
        assert (
            connection.execute(
                "SELECT migration_status FROM migration_history WHERE schema_version=999 ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            == "running"
        )


def test_known_unfinished_migration_without_schema_row_stays_fail_closed(tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    migration_name = "保存昂貴 POST 的 Idempotency request fingerprint"
    with database.session() as connection:
        connection.execute(
            """
            INSERT INTO migration_history(
                schema_version,migration_name,migration_started_at,migration_status
            ) VALUES (42,?,datetime('now'),'running')
            """,
            (migration_name,),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version=42")

    with pytest.raises(MigrationError, match="MIGRATION-002"):
        migrate(database)
    with database.session() as connection:
        assert (
            connection.execute(
                "SELECT migration_status FROM migration_history WHERE schema_version=42 AND migration_name=? ORDER BY id DESC LIMIT 1",
                (migration_name,),
            ).fetchone()[0]
            == "running"
        )


@pytest.mark.parametrize(
    ("provenance", "initial_value", "initial_updated_by", "expected_value", "expected_updated_by"),
    [
        ("history_300", 300, None, 300, "operator-300"),
        ("snapshot_300", 300, None, 300, "operator-300"),
        ("history_300_then_21600", 300, None, 21600, None),
        ("history_600", 600, "operator-600", 600, "operator-600"),
        ("none", 300, None, 21600, None),
        ("fresh_default", 21600, None, 21600, None),
        ("conflicting", 300, None, 21600, None),
        ("malformed", 300, None, 21600, None),
    ],
)
def test_migration_43_preserves_diagnostics_cache_provenance(
    monkeypatch,
    tmp_path,
    provenance,
    initial_value,
    initial_updated_by,
    expected_value,
    expected_updated_by,
):
    database = Database(tmp_path / f"{provenance}.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:38])
    migrate(database)
    key = "system.diagnostics_cache_seconds"

    with database.session() as connection:
        connection.execute(
            """
            INSERT INTO settings(key,category,value_json,value_type,updated_by,updated_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                key,
                "Log 與診斷",
                json.dumps(initial_value),
                "integer",
                initial_updated_by,
                "2026-01-01T00:00:00+00:00",
            ),
        )

        if provenance == "history_300":
            connection.execute(
                """
                INSERT INTO setting_history(
                    key,changed_at,changed_by,old_value_summary,new_value_summary,source_ip,requires_restart
                ) VALUES (?,?,?,?,?,?,0)
                """,
                (key, "2026-01-01T00:00:00+00:00", "operator-300", "21600", "300", "127.0.0.1"),
            )
        elif provenance == "snapshot_300":
            connection.execute(
                """
                INSERT INTO settings_snapshots(
                    id,created_at,actor_id,source_ip,reason,before_json,after_json,changed_keys_json,
                    schema_version,application_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "snapshot-300",
                    "2026-01-01T00:00:00+00:00",
                    "operator-300",
                    "127.0.0.1",
                    "legacy explicit default",
                    json.dumps({key: 21600}),
                    json.dumps({key: 300}),
                    json.dumps([key]),
                    38,
                    "test",
                ),
            )
            connection.execute(
                """
                INSERT INTO settings_snapshot_items(snapshot_id,key,old_value_json,new_value_json,restored_default)
                VALUES (?,?,?,?,0)
                """,
                ("snapshot-300", key, "21600", "300"),
            )
        elif provenance == "history_300_then_21600":
            connection.executemany(
                """
                INSERT INTO setting_history(
                    key,changed_at,changed_by,old_value_summary,new_value_summary,source_ip,requires_restart
                ) VALUES (?,?,?,?,?,?,0)
                """,
                [
                    (key, "2026-01-01T00:00:00+00:00", "operator-300", "21600", "300", "127.0.0.1"),
                    (key, "2026-01-02T00:00:00+00:00", "operator-21600", "300", "21600", "127.0.0.1"),
                ],
            )
        elif provenance == "conflicting":
            connection.execute(
                """
                INSERT INTO setting_history(
                    key,changed_at,changed_by,old_value_summary,new_value_summary,source_ip,requires_restart
                ) VALUES (?,?,?,?,?,?,0)
                """,
                (key, "2026-01-01T00:00:00+00:00", "operator-300", "21600", "300", "127.0.0.1"),
            )
            connection.execute(
                """
                INSERT INTO settings_snapshots(
                    id,created_at,actor_id,source_ip,reason,before_json,after_json,changed_keys_json,
                    schema_version,application_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "snapshot-conflict",
                    "2026-01-01T00:00:00+00:00",
                    "operator-21600",
                    "127.0.0.1",
                    "conflict",
                    json.dumps({key: 300}),
                    json.dumps({key: 21600}),
                    json.dumps([key]),
                    38,
                    "test",
                ),
            )
            connection.execute(
                """
                INSERT INTO settings_snapshot_items(snapshot_id,key,old_value_json,new_value_json,restored_default)
                VALUES (?,?,?,?,0)
                """,
                ("snapshot-conflict", key, "300", "21600"),
            )
        elif provenance == "malformed":
            connection.execute(
                """
                INSERT INTO setting_history(
                    key,changed_at,changed_by,old_value_summary,new_value_summary,source_ip,requires_restart
                ) VALUES (?,?,?,?,?,?,0)
                """,
                (key, "2026-01-01T00:00:00+00:00", "operator-unknown", "21600", "not-json", "127.0.0.1"),
            )

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    assert migrate(database) == list(range(39, CURRENT_SCHEMA_VERSION + 1))
    with database.session() as connection:
        row = connection.execute(
            "SELECT value_json,updated_by FROM settings WHERE key=?", (key,)
        ).fetchone()
    assert json.loads(row["value_json"]) == expected_value
    assert row["updated_by"] == expected_updated_by
    assert database.schema_version() == CURRENT_SCHEMA_VERSION
    assert database.integrity_check() == "ok"


def test_concurrent_migrations_are_serialized(tmp_path):
    database = Database(tmp_path / "inktime.db")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: migrate(database), range(2)))
        assert sorted(results, key=len) == [[], list(range(1, CURRENT_SCHEMA_VERSION + 1))]
    assert database.integrity_check() == "ok"


def test_capture_date_backfill_is_cross_process_singleflight(tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) "
            "VALUES ('lib','L','/photos',datetime('now'),datetime('now'))"
        )
        connection.executemany(
            "INSERT INTO photos(id,library_id,relative_path,status,captured_at,created_at,updated_at) "
            "VALUES (?,'lib',?,'discovered','2024-02-29T12:30:00',datetime('now'),datetime('now'))",
            [(f"photo-{index:04d}", f"{index}.jpg") for index in range(500)],
        )

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_run_capture_date_backfill,
            args=(str(database.path), start, results),
        )
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)
        assert not process.is_alive()
        assert process.exitcode == 0

    outcomes = [results.get(timeout=5) for _index in processes]
    assert all("error" not in outcome for outcome in outcomes)
    assert sorted(outcome["processed"] for outcome in outcomes) == [0, 500]
    with database.session() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM photos WHERE capture_date_status='pending'").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM photos WHERE captured_date='2024-02-29' "
                "AND captured_month_day='02-29' AND capture_date_status='valid'"
            ).fetchone()[0]
            == 500
        )


def test_capture_date_backfill_releases_operation_lock_after_exception(monkeypatch, tmp_path):
    from inktime.app.domain.photos import dates

    database = Database(tmp_path / "inktime.db")
    migrate(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) "
            "VALUES ('lib','L','/photos',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO photos(id,library_id,relative_path,status,captured_at,created_at,updated_at) "
            "VALUES ('photo','lib','a.jpg','discovered','2024-02-29',datetime('now'),datetime('now'))"
        )

    original = dates.materialized_capture_fields

    def fail(_value):
        raise RuntimeError("synthetic parse failure")

    monkeypatch.setattr(dates, "materialized_capture_fields", fail)
    with pytest.raises(RuntimeError, match="synthetic parse failure"):
        migrations_module.backfill_photo_capture_dates(database)
    monkeypatch.setattr(dates, "materialized_capture_fields", original)
    assert migrations_module.backfill_photo_capture_dates(database)["processed"] == 1


def test_wal_allows_reader_while_cross_process_writer_boundary_serializes_writes(tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def first_writer():
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES ('one','一','/one',datetime('now'),datetime('now'))"
            )
            first_started.set()
            assert release_first.wait(5)

    def second_writer():
        second_started.set()
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES ('two','二','/two',datetime('now'),datetime('now'))"
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_writer)
        assert first_started.wait(5)
        second = executor.submit(second_writer)
        assert second_started.wait(5)
        with database.session() as reader:
            assert reader.execute("SELECT COUNT(*) FROM libraries").fetchone()[0] == 0
        assert not second.done()
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)
    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM libraries").fetchone()[0] == 2


def test_existing_empty_font_setting_moves_to_builtin_iansui(monkeypatch, tmp_path):
    database = Database(tmp_path / "inktime.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:8])
    assert migrate(database) == [1, 2, 3, 4, 5, 6, 7, 8]
    with database.session() as connection:
        connection.execute(
            "INSERT INTO settings(key,category,value_json,value_type,requires_restart,updated_at) "
            "VALUES ('render.font_path','渲染設定','\"\"','string',0,datetime('now'))"
        )

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:9])
    assert migrate(database) == [9]
    with database.session() as connection:
        value = connection.execute("SELECT value_json FROM settings WHERE key='render.font_path'").fetchone()[
            0
        ]
    assert value == '"builtin:iansui"'


def test_wal_foreign_keys_busy_timeout_and_synchronous_are_applied(tmp_path):
    database = Database(tmp_path / "inktime.db", busy_timeout_ms=12_345)
    migrate(database)
    with database.session() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 12_345
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at)
                VALUES ('orphan','missing-library','orphan.jpg','discovered',datetime('now'),datetime('now'))
                """
            )


def test_unfinished_migration_stops_startup_before_new_schema_writes(tmp_path):
    database = Database(tmp_path / "inktime.db")
    migrate(database)
    with database.session() as connection:
        connection.execute(
            """
            INSERT INTO migration_history(
                schema_version,migration_name,migration_started_at,migration_status,backup_path
                ) VALUES (999,'中斷測試',datetime('now'),'running','/data/backups/pre-migration.sqlite3')
            """
        )

    with pytest.raises(MigrationError, match="MIGRATION-002.*停止啟動"):
        migrate(database)
    with database.session() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=999").fetchone()[0] == 0
        )


def test_v10_photo_state_and_analysis_survive_scheduler_upgrade(monkeypatch, tmp_path):
    database = Database(tmp_path / "inktime.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:10])
    migrate(database)
    with database.session() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES ('lib','舊相簿','/photos',datetime('now'),datetime('now'))"
        )
        connection.execute(
            """
            INSERT INTO photos(
                id,library_id,relative_path,file_size,modified_time,sha256,status,favorite,created_at,updated_at
            ) VALUES ('photo','lib','old.jpg',123,456,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','analyzed',1,datetime('now'),datetime('now'))
            """
        )
        connection.execute(
            """
            INSERT INTO photo_analysis(
                photo_id,schema_version,stage,caption,types_json,raw_json,created_at
            ) VALUES ('photo',1,'high','舊描述','[]','{}',datetime('now'))
            """
        )

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    assert migrate(database, tmp_path / "backups") == list(range(11, CURRENT_SCHEMA_VERSION + 1))
    with database.session() as connection:
        photo = connection.execute(
            "SELECT favorite,status,lifecycle_status,metadata_status,local_features_status FROM photos WHERE id='photo'"
        ).fetchone()
        caption = connection.execute("SELECT caption FROM photo_analysis WHERE photo_id='photo'").fetchone()[
            0
        ]
    assert tuple(photo) == (1, "analyzed", "active", "complete", "complete")
    assert caption == "舊描述"


def test_migration_21_upgrades_v20_webhooks_idempotently(monkeypatch, tmp_path):
    database = Database(tmp_path / "inktime.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:20])
    migrate(database)
    with database.session() as connection:
        connection.execute(
            """
            INSERT INTO device_notifications(
                kind,level,title,message,webhook_status,created_at
            ) VALUES ('test','info','舊通知','backfill','pending',datetime('now'))
            """
        )
        notification_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    assert migrate(database, tmp_path / "backups") == list(range(21, CURRENT_SCHEMA_VERSION + 1))
    assert migrate(database, tmp_path / "backups") == []
    assert database.integrity_check() == "ok"
    with database.session() as connection:
        row = connection.execute(
            "SELECT webhook_idempotency_key,webhook_claimed_until FROM device_notifications WHERE id=?",
            (notification_id,),
        ).fetchone()
        indexes = {
            str(index["name"]): int(index["unique"])
            for index in connection.execute("PRAGMA index_list(device_notifications)")
        }
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO device_notifications(
                    kind,level,title,message,webhook_status,
                    webhook_idempotency_key,created_at
                ) VALUES ('test','info','重複','duplicate','pending',?,datetime('now'))
                """,
                (row["webhook_idempotency_key"],),
            )
    assert row["webhook_idempotency_key"] == f"legacy:{notification_id}"
    assert row["webhook_claimed_until"] is None
    assert indexes["idx_device_notifications_idempotency"] == 1
    assert indexes["idx_device_notifications_claim"] == 0


def test_migration_24_updates_caption_defaults_only_as_one_legacy_set(monkeypatch, tmp_path):
    database = Database(tmp_path / "inktime.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:23])
    migrate(database)
    keys = (
        "analysis.side_caption_min_chars",
        "analysis.side_caption_target_chars",
        "analysis.side_caption_max_chars",
    )
    with database.session() as connection:
        connection.executemany(
            "INSERT INTO settings(key,category,value_json,value_type,updated_at) VALUES (?,'analysis',?,'integer',datetime('now'))",
            [(keys[0], "10"), (keys[1], "30"), (keys[2], "42")],
        )
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    migrate(database)
    with database.session() as connection:
        values = [
            connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()[0]
            for key in keys
        ]
    assert values == ["10", "30", "42"]

    second = Database(tmp_path / "second.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:23])
    migrate(second)
    with second.session() as connection:
        connection.executemany(
            "INSERT INTO settings(key,category,value_json,value_type,updated_at) VALUES (?,'analysis',?,'integer',datetime('now'))",
            [(keys[0], "10"), (keys[1], "22"), (keys[2], "42")],
        )
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS)
    migrate(second)
    with second.session() as connection:
        values = [
            connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()[0]
            for key in keys
        ]
    assert values == ["8", "12", "16"]
