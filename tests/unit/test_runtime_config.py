from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from inktime.app.core.runtime_config import RuntimeConfig, RuntimeConfigurationError


def test_explicit_values_override_environment_and_relative_paths_share_one_base(tmp_path: Path):
    config = RuntimeConfig.from_sources(
        environ={
            "INKTIME_PORT": "9999",
            "INKTIME_DATA_DIR": "environment-data",
            "INKTIME_ENABLE_LEGACY_WEBUI": "true",
        },
        base_dir=tmp_path,
        environment="test",
        testing=True,
        development=False,
        port=8765,
        data_dir="explicit-data",
        database_path="database/inktime.sqlite",
        photo_dir="photos",
        release_dir="releases",
        backup_dir="backups",
        cache_dir="cache",
        legacy_enabled=False,
        cookie_secure=False,
    )

    assert config.port == 8765
    assert config.data_dir == (tmp_path / "explicit-data").resolve()
    assert config.database_path == (tmp_path / "database/inktime.sqlite").resolve()
    assert config.photo_dir == (tmp_path / "photos").resolve()
    assert config.legacy_enabled is False


def test_environment_precedes_safe_defaults(tmp_path: Path):
    config = RuntimeConfig.from_sources(
        environ={
            "INKTIME_ENVIRONMENT": "test",
            "INKTIME_TESTING": "true",
            "INKTIME_DEVELOPMENT": "false",
            "INKTIME_DATA_DIR": "runtime",
            "INKTIME_WORKER_CONCURRENCY": "4",
            "INKTIME_TIMEZONE": "Asia/Tokyo",
        },
        base_dir=tmp_path,
    )

    assert config.data_dir == (tmp_path / "runtime").resolve()
    assert config.database_path == (tmp_path / "runtime/inktime.db").resolve()
    assert config.worker_concurrency == 4
    assert config.timezone == "Asia/Tokyo"


@pytest.mark.parametrize(
    ("field", "value"),
    (("port", "0"), ("proxy_trust", "11"), ("worker_concurrency", "0"), ("timezone", "Mars/Olympus")),
)
def test_invalid_typed_values_fail_closed(tmp_path: Path, field: str, value: str):
    arguments = {
        "environment": "test",
        "testing": True,
        "development": False,
        "data_dir": tmp_path / "data",
        "cookie_secure": False,
        field: value,
    }
    with pytest.raises(RuntimeConfigurationError):
        RuntimeConfig.from_sources(environ={}, base_dir=tmp_path, **arguments)


def test_production_rejects_repository_data_default():
    with pytest.raises(RuntimeConfigurationError, match="Production"):
        RuntimeConfig.from_sources(
            environ={},
            environment="production",
            data_dir=Path(__file__).resolve().parents[2] / "data",
            testing=False,
            development=False,
            cookie_secure=True,
        )


def test_runtime_config_is_immutable_and_never_contains_secret(tmp_path: Path):
    secret = "sk-must-never-appear"
    config = RuntimeConfig.from_sources(
        environ={"INKTIME_SECRET_KEY": secret},
        base_dir=tmp_path,
        environment="test",
        testing=True,
        development=False,
        data_dir=tmp_path / "data",
        cookie_secure=False,
    )
    with pytest.raises(FrozenInstanceError):
        config.port = 9999  # type: ignore[misc]
    summary = json.dumps(config.diagnostic_summary())
    assert secret not in repr(config)
    assert secret not in summary
    assert str(config.data_dir) not in summary
    assert str(config.database_path) not in summary
