from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


def test_readers_and_writers_share_wal_without_unhandled_lock(app):
    database = app.extensions["inktime_database"]

    def writer(index: int) -> None:
        with database.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO feature_flags(key,enabled,description,updated_at) VALUES (?,1,'test',datetime('now'))",
                (f"concurrency-{index}",),
            )

    def reader(_index: int) -> int:
        with database.session() as connection:
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
            return int(connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0])

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda index: writer(index) if index % 2 else reader(index), range(40)))
    assert database.integrity_check() == "ok"
    metrics = database.observability()
    assert metrics["writer_lock_acquisitions"] > 0
    assert metrics["writer_lock_wait_ms"] >= 0
    assert metrics["wal_size_bytes"] >= 0
