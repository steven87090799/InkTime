from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import inktime.app.db.migrations as migrations_module
from inktime.app.db import Database, MigrationError, migrate
from inktime.app.db.migrations import Migration, MIGRATIONS


def test_fresh_database_is_migrated(tmp_path):
    database = Database(tmp_path / "inktime.db")
    assert migrate(database) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
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
    } <= tables
    assert tuple(history) == (13, 13)


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


def test_history_completion_failure_keeps_running_marker_and_stops_restart(
    monkeypatch, tmp_path
):
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
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='committed_schema'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=999"
        ).fetchone()
        assert connection.execute(
            "SELECT migration_status FROM migration_history WHERE schema_version=999"
        ).fetchone()[0] == "running"
    with pytest.raises(MigrationError, match="MIGRATION-002"):
        migrate(database, tmp_path / "backups")


def test_concurrent_migrations_are_serialized(tmp_path):
    database = Database(tmp_path / "inktime.db")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: migrate(database), range(2)))
    assert sorted(results, key=len) == [[], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]]
    assert database.integrity_check() == "ok"


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
        value = connection.execute(
            "SELECT value_json FROM settings WHERE key='render.font_path'"
        ).fetchone()[0]
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
                ) VALUES (14,'中斷測試',datetime('now'),'running','/data/backups/pre-migration.sqlite3')
            """
        )

    with pytest.raises(MigrationError, match="MIGRATION-002.*停止啟動"):
        migrate(database)
    with database.session() as connection:
        assert connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version=14"
        ).fetchone()[0] == 0


def test_v10_photo_state_and_analysis_survive_scheduler_upgrade(monkeypatch, tmp_path):
    database = Database(tmp_path / "inktime.db")
    monkeypatch.setattr("inktime.app.db.migrations.MIGRATIONS", MIGRATIONS[:-3])
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
    assert migrate(database, tmp_path / "backups") == [11, 12, 13]
    with database.session() as connection:
        photo = connection.execute(
            "SELECT favorite,status,lifecycle_status,metadata_status,local_features_status FROM photos WHERE id='photo'"
        ).fetchone()
        caption = connection.execute(
            "SELECT caption FROM photo_analysis WHERE photo_id='photo'"
        ).fetchone()[0]
    assert tuple(photo) == (1, "analyzed", "active", "complete", "complete")
    assert caption == "舊描述"
