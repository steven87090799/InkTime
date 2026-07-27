from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys

import pytest

from inktime.app.core.locks import FcntlLockProvider, LockUnavailableError
from inktime.app.bootstrap import bootstrap_services
from inktime.app.core.runtime_config import RuntimeConfig


def test_fcntl_provider_supports_cross_process_lock_on_linux_and_darwin(tmp_path: Path):
    provider = FcntlLockProvider()
    with provider.exclusive(tmp_path / "writer.lock", timeout_seconds=0.1) as handle:
        assert handle.fileno() >= 0


def test_windows_native_fails_with_explicit_docker_guidance(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(LockUnavailableError, match="Linux Docker"):
        with FcntlLockProvider().exclusive(tmp_path / "runtime.lock"):
            pass


def test_all_existing_file_locks_share_the_lazy_os_adapter():
    from inktime.app.core import locks
    from inktime.app.db import connection, migrations
    from inktime.app.domain.photos import thumbnails

    assert connection.fcntl is locks.fcntl
    assert migrations.fcntl is locks.fcntl
    assert thumbnails.fcntl is locks.fcntl


def test_bootstrap_accepts_injected_lock_provider(tmp_path: Path):
    class FakeLockProvider:
        def __init__(self) -> None:
            self.paths: list[Path] = []

        @contextmanager
        def exclusive(self, path: Path, timeout_seconds: float = 10.0):
            self.paths.append(path)
            yield object()

    config = RuntimeConfig.from_sources(
        environ={},
        environment="development",
        development=True,
        testing=False,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "database.sqlite",
        photo_dir=tmp_path / "photos",
        release_dir=tmp_path / "releases",
        backup_dir=tmp_path / "backups",
        cache_dir=tmp_path / "cache",
        legacy_enabled=False,
        cookie_secure=False,
    )
    provider = FakeLockProvider()
    container = bootstrap_services(config, role="scheduler", lock_provider=provider)
    try:
        assert provider.paths == [tmp_path / "data/session.key.lock"]
    finally:
        container.close()
