from __future__ import annotations

from pathlib import Path

from inktime.app.db import Database, migrate
from scripts.performance_100k import seed_photo_metadata


def test_seed_photo_metadata_uses_explicit_bounded_transactions(tmp_path: Path, monkeypatch):
    database = Database(tmp_path / "performance.db")
    migrate(database)
    statements: list[str] = []
    original_connect = database.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "connect", traced_connect)
    library_id, rss_peak = seed_photo_metadata(
        database,
        photo_count=2_501,
        batch_size=1_000,
    )

    assert rss_peak == 0
    assert statements.count("BEGIN IMMEDIATE") == 4
    assert statements.count("COMMIT") == 4
    with database.session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM photos WHERE library_id=?", (library_id,)
        ).fetchone()[0] == 2_501
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_seed_photo_metadata_rejects_unbounded_batch_configuration(tmp_path: Path):
    database = Database(tmp_path / "performance.db")
    migrate(database)

    for photo_count, batch_size in ((-1, 1_000), (1, 0)):
        try:
            seed_photo_metadata(database, photo_count=photo_count, batch_size=batch_size)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid benchmark batch configuration must fail closed")
