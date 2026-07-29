from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from inktime.app.bootstrap import bootstrap_services
from inktime.app.core.runtime_config import RuntimeConfig
from inktime.app.factory import create_app


def _config(root: Path, *, legacy_enabled: bool = False) -> RuntimeConfig:
    return RuntimeConfig.from_sources(
        environ={},
        environment="test",
        testing=True,
        development=False,
        data_dir=root / "data",
        database_path=root / "database.sqlite",
        photo_dir=root / "photos",
        release_dir=root / "releases",
        backup_dir=root / "backups",
        cache_dir=root / "cache",
        legacy_enabled=legacy_enabled,
        cookie_secure=False,
    )


def test_factory_module_import_has_no_filesystem_or_legacy_side_effect(tmp_path: Path):
    script = (
        "import sys; from inktime.app.factory import create_app; "
        "assert 'legacy_server' not in sys.modules; "
        "assert 'inktime.app.legacy.blueprint' not in sys.modules; print(create_app.__name__)"
    )
    result = subprocess.run(  # noqa: S603 -- fixed current interpreter and constant script.
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "create_app"
    assert list(tmp_path.iterdir()) == []


def test_two_app_instances_have_isolated_database_cache_extensions_and_config(tmp_path: Path):
    first = create_app(_config(tmp_path / "first"))
    second = create_app(_config(tmp_path / "second"))
    try:
        assert first is not second
        assert first.extensions["inktime_database"] is not second.extensions["inktime_database"]
        assert first.extensions["inktime_thumbnail_cache"] is not second.extensions["inktime_thumbnail_cache"]
        assert first.extensions["inktime_runtime_config"] is not second.extensions["inktime_runtime_config"]
        first.extensions["inktime_auth_repository"].create_user("first", "password-long")
        assert second.extensions["inktime_auth_repository"].count_users() == 0
    finally:
        first.extensions["inktime_service_container"].close()
        second.extensions["inktime_service_container"].close()


def test_initialized_app_rejects_second_platform_initialization(tmp_path: Path):
    from inktime.app.platform import initialize_platform

    app = create_app(_config(tmp_path / "app"))
    try:
        with pytest.raises(RuntimeError, match="不得.*重複"):
            initialize_platform(
                app,
                database_path=tmp_path / "other.sqlite",
                data_dir=tmp_path / "other-data",
                release_dir=tmp_path / "other-releases",
                testing=True,
            )
        assert not (tmp_path / "other-data").exists()
    finally:
        app.extensions["inktime_service_container"].close()


def test_server_app_uses_factory_and_gunicorn_contract_in_isolated_runtime(tmp_path: Path):
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "INKTIME_ENVIRONMENT": "test",
        "INKTIME_TESTING": "true",
        "INKTIME_DEVELOPMENT": "false",
        "INKTIME_DATA_DIR": str(tmp_path / "data"),
        "INKTIME_DATABASE": str(tmp_path / "db.sqlite"),
        "INKTIME_PHOTO_DIR": str(tmp_path / "photos"),
        "INKTIME_RELEASE_DIR": str(tmp_path / "releases"),
        "INKTIME_BACKUP_DIR": str(tmp_path / "backups"),
        "INKTIME_CACHE_DIR": str(tmp_path / "cache"),
    }
    result = subprocess.run(  # noqa: S603 -- fixed current interpreter and constant script.
        [sys.executable, "-c", "from server import app; print(app.import_name, app.url_map)"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "inktime" in result.stdout
    assert "/api/device/v1/releases/latest" in result.stdout
    assert "legacy_server" not in result.stdout


def test_worker_and_scheduler_bootstrap_do_not_import_web_or_server(tmp_path: Path):
    worker = bootstrap_services(_config(tmp_path / "worker"), role="worker")
    scheduler = bootstrap_services(_config(tmp_path / "scheduler"), role="scheduler")
    try:
        assert worker.role == "worker"
        assert scheduler.role == "scheduler"
        assert "inktime_auth_repository" not in worker.extensions
        assert "inktime_render_service" not in scheduler.extensions
        assert "server" not in sys.modules
    finally:
        worker.close()
        scheduler.close()


def test_web_worker_and_scheduler_share_the_exact_resolved_runtime_config(tmp_path: Path):
    config = _config(tmp_path / "shared")
    web = create_app(config)
    worker = bootstrap_services(config, role="worker")
    scheduler = bootstrap_services(config, role="scheduler")
    try:
        assert web.extensions["inktime_runtime_config"] is config
        assert worker.extensions["inktime_runtime_config"] is config
        assert scheduler.extensions["inktime_runtime_config"] is config
    finally:
        web.extensions["inktime_service_container"].close()
        worker.close()
        scheduler.close()
