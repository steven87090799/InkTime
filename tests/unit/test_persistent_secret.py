from dataclasses import replace
import os
import sqlite3

import pytest

from inktime.app.bootstrap import _persistent_secret
from inktime.app.core.locks import FcntlLockProvider
from inktime.app.core.runtime_config import RuntimeConfig


def config_for(tmp_path):
    return replace(RuntimeConfig.from_sources(environ={}, testing=True),
                   testing=False, data_dir=tmp_path, database_path=tmp_path / "test.db")


def test_secret_is_durable_private_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("INKTIME_SECRET_KEY", raising=False)
    config = config_for(tmp_path)
    calls = []
    original = os.fsync

    def fsync(descriptor):
        calls.append(os.fstat(descriptor).st_mode)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    value = _persistent_secret(config, FcntlLockProvider())
    assert len(calls) == 2
    assert (tmp_path / "session.key").stat().st_mode & 0o777 == 0o600
    assert _persistent_secret(config, FcntlLockProvider()) == value
    assert not list(tmp_path.glob(".session-key-*"))


@pytest.mark.parametrize("existing", ["empty_key", "data"])
def test_missing_or_empty_key_with_existing_state_fails_closed(tmp_path, monkeypatch, existing):
    monkeypatch.delenv("INKTIME_SECRET_KEY", raising=False)
    config = config_for(tmp_path)
    if existing == "empty_key":
        (tmp_path / "session.key").write_text("")
    else:
        with sqlite3.connect(config.database_path) as connection:
            connection.execute("CREATE TABLE devices(id TEXT)")
            connection.execute("INSERT INTO devices VALUES ('paired')")
    with pytest.raises(RuntimeError, match="SESSION-002"):
        _persistent_secret(config, FcntlLockProvider())


def test_secret_file_fsync_failure_never_publishes_key(tmp_path, monkeypatch):
    monkeypatch.delenv("INKTIME_SECRET_KEY", raising=False)

    def fail(_descriptor):
        raise OSError("forced sync failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="forced sync failure"):
        _persistent_secret(config_for(tmp_path), FcntlLockProvider())
    assert not (tmp_path / "session.key").exists()
    assert not list(tmp_path.glob(".session-key-*"))
