from __future__ import annotations

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
    database = Database(tmp_path / "inktime.db")
    assert migrate(database) == list(range(1, 28))
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
    } <= tables
    assert tuple(history) == (27, 27)
    with database.session() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
    assert {"normalized_username", "session_version", "disabled_at"} <= columns
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
    assert migrate(database, tmp_path / "backups") == [26, 27]
    assert migrate(database, tmp_path / "backups") == []
    assert database.integrity_check() == "ok"


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
    with pytest.raises(MigrationError, match="MIGRATION-002"):
        migrate(database, tmp_path / "backups")


def test_concurrent_migrations_are_serialized(tmp_path):
    database = Database(tmp_path / "inktime.db")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: migrate(database), range(2)))
        assert sorted(results, key=len) == [[], list(range(1, 28))]
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
    assert migrate(database, tmp_path / "backups") == list(range(11, 28))
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
    assert migrate(database, tmp_path / "backups") == [21, 22, 23, 24, 25, 26, 27]
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
