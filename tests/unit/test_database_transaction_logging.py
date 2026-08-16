from __future__ import annotations

import logging
import sqlite3

import pytest

from inktime.app.db import Database
from inktime.app.repositories.devices import DeviceRateLimitError


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "transaction-logging.sqlite")
    with database.session() as connection:
        connection.execute("CREATE TABLE writes(value TEXT NOT NULL)")
    return database


def _stored_values(database: Database) -> list[str]:
    with database.session() as connection:
        return [str(row[0]) for row in connection.execute("SELECT value FROM writes")]


def _db_failure_records(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "db_transaction_failed"
        and getattr(record, "error_code", "") == "DB-TX-001"
    ]


def test_device_rate_limit_rolls_back_and_propagates_without_db_error(tmp_path, caplog):
    database = _database(tmp_path)
    caplog.set_level(logging.ERROR, logger="inktime.app.db.connection")

    with pytest.raises(DeviceRateLimitError, match="rate limited"):
        with database.transaction(operation="test.device_rate_limit") as connection:
            connection.execute("INSERT INTO writes(value) VALUES ('domain')")
            raise DeviceRateLimitError("rate limited", retry_after_seconds=17)

    assert _stored_values(database) == []
    assert _db_failure_records(caplog) == []


def test_sqlite_failure_rolls_back_and_keeps_db_error(tmp_path, caplog):
    database = _database(tmp_path)
    caplog.set_level(logging.ERROR, logger="inktime.app.db.connection")

    with pytest.raises(sqlite3.OperationalError):
        with database.transaction(operation="test.sqlite_failure") as connection:
            connection.execute("INSERT INTO writes(value) VALUES ('sqlite')")
            connection.execute("INSERT INTO missing_table(value) VALUES ('fail')")

    assert _stored_values(database) == []
    assert len(_db_failure_records(caplog)) == 1


def test_unexpected_exception_rolls_back_and_is_not_swallowed(tmp_path, caplog):
    database = _database(tmp_path)
    caplog.set_level(logging.ERROR, logger="inktime.app.db.connection")

    with pytest.raises(RuntimeError, match="unexpected failure"):
        with database.transaction(operation="test.unexpected") as connection:
            connection.execute("INSERT INTO writes(value) VALUES ('unexpected')")
            raise RuntimeError("unexpected failure")

    assert _stored_values(database) == []
    assert len(_db_failure_records(caplog)) == 1
