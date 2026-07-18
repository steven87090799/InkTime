from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from inktime.app.db import Database, MigrationError, migrate
from inktime.app.db.migrations import Migration, MIGRATIONS


def test_fresh_database_is_migrated(tmp_path):
    database = Database(tmp_path / "inktime.db")
    assert migrate(database) == [1, 2, 3, 4, 5]
    assert database.integrity_check() == "ok"
    with database.session() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "photos",
        "jobs",
        "job_items",
        "api_usage",
        "users",
        "devices",
        "scoring_rule_versions",
    } <= tables


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
    assert table is None
    assert recorded is None


def test_concurrent_migrations_are_serialized(tmp_path):
    database = Database(tmp_path / "inktime.db")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: migrate(database), range(2)))
    assert sorted(results, key=len) == [[], [1, 2, 3, 4, 5]]
    assert database.integrity_check() == "ok"
