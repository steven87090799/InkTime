from __future__ import annotations

from pathlib import Path

import pytest

from inktime.app.core.preflight import PreflightError, run_production_preflight
from inktime.app.core.runtime_config import RuntimeConfig


class LocalFilesystem:
    def mountinfo(self) -> str:
        return "1 0 0:1 / / rw - ext4 /dev/root rw"


def _config(tmp_path: Path, **overrides) -> RuntimeConfig:
    arguments = {
        "environment": "production",
        "testing": False,
        "development": False,
        "data_dir": tmp_path / "runtime",
        "database_path": tmp_path / "runtime/inktime.db",
        "photo_dir": tmp_path / "photos",
        "release_dir": tmp_path / "runtime/releases",
        "backup_dir": tmp_path / "runtime/backups",
        "cache_dir": tmp_path / "runtime/cache",
        "public_url": "https://inktime.home.example.net",
        "cookie_secure": True,
        "allow_insecure_http": False,
    }
    arguments.update(overrides)
    return RuntimeConfig.from_sources(environ={}, base_dir=tmp_path, **arguments)


def test_production_https_configuration_is_accepted(tmp_path):
    result = run_production_preflight(_config(tmp_path), adapter=LocalFilesystem())
    assert result.healthy


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"public_url": "http://inktime.home.example.net", "cookie_secure": True},
            "COOKIE_SECURE=0",
        ),
        (
            {
                "public_url": "http://inktime.home.example.net",
                "cookie_secure": False,
                "allow_insecure_http": False,
            },
            "ALLOW_INSECURE_HTTP=1",
        ),
        ({"public_url": "https://inktime.example.com"}, "範例網域"),
        ({"public_url": "https://localhost"}, "localhost"),
        ({"public_url": "https://user:secret@inktime.test.net"}, "不可包含帳密"),
    ],
)
def test_invalid_public_url_and_cookie_combinations_fail_with_diagnostics(
    tmp_path,
    overrides,
    message,
):
    with pytest.raises(PreflightError, match=message):
        run_production_preflight(
            _config(tmp_path, **overrides),
            adapter=LocalFilesystem(),
        )
