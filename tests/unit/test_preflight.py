from __future__ import annotations

from pathlib import Path

import pytest

from inktime.app.core.preflight import (
    PreflightError,
    run_production_preflight,
    validate_lan_environment,
)
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
        "public_url": "https://inktime.home.acme.dev",
        "cookie_secure": True,
        "allow_insecure_http": False,
    }
    arguments.update(overrides)
    return RuntimeConfig.from_sources(environ={}, base_dir=tmp_path, **arguments)


def test_production_https_configuration_is_accepted(tmp_path):
    result = run_production_preflight(_config(tmp_path), adapter=LocalFilesystem())
    assert result.healthy
    assert result.transport == "https"
    assert result.security_state == "secure"
    assert result.tls_enabled is True
    assert result.secure_cookie is True


@pytest.mark.parametrize(
    "public_url",
    [
        "http://192.168.1.100:8765",
        "http://127.0.0.1:8765",
        "http://169.254.10.20:8765",
        "http://inktime.local:8765",
        "http://inktime:8765",
    ],
)
def test_trusted_lan_production_is_explicitly_degraded(tmp_path, public_url):
    result = run_production_preflight(
        _config(
            tmp_path,
            public_url=public_url,
            cookie_secure=False,
            allow_insecure_http=True,
            proxy_trust=0,
        ),
        adapter=LocalFilesystem(),
        mode="lan",
    )
    assert result.healthy is False
    assert result.transport == "trusted-lan-http"
    assert result.security_state == "degraded"
    assert result.tls_enabled is False
    assert result.secure_cookie is False
    assert result.summary()["status"] == "degraded"


@pytest.mark.parametrize(
    "public_url",
    [
        "http://public.example.net:8765",
        "http://8.8.8.8:8765",
        "http://inktime.example.com:8765",
    ],
)
def test_lan_mode_rejects_public_http_hosts(tmp_path, public_url):
    with pytest.raises(PreflightError, match="PREFLIGHT-LAN-003"):
        run_production_preflight(
            _config(
                tmp_path,
                public_url=public_url,
                cookie_secure=False,
                allow_insecure_http=True,
                proxy_trust=0,
            ),
            adapter=LocalFilesystem(),
            mode="lan",
        )


def _lan_environ(tmp_path: Path) -> dict[str, str]:
    return {
        "INKTIME_ENVIRONMENT": "production",
        "INKTIME_ALLOW_INSECURE_HTTP": "1",
        "INKTIME_COOKIE_SECURE": "0",
        "INKTIME_PROXY_TRUST": "0",
        "INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE": "0",
        "INKTIME_DATA_PATH": str(tmp_path / "data"),
        "INKTIME_PHOTO_PATH": str(tmp_path / "photos"),
        "INKTIME_IMAGE_TAG": "0123456789abcdef",
        "INKTIME_GIT_REVISION": "0123456789abcdef",
    }


def test_lan_environment_requires_production_paths_identity_and_readonly_photos(tmp_path):
    validate_lan_environment(_lan_environ(tmp_path), "- ${INKTIME_PHOTO_PATH}:/photos:ro")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("INKTIME_PHOTO_PATH", "/srv/simulation_photos", "PREFLIGHT-LAN-PATH-002"),
        ("INKTIME_DATA_PATH", "/CHANGE_ME/data", "PREFLIGHT-LAN-PATH-001"),
        ("INKTIME_IMAGE_TAG", "local", "PREFLIGHT-LAN-BUILD-001"),
        ("INKTIME_PROXY_TRUST", "1", "PREFLIGHT-LAN-ENV-001"),
    ],
)
def test_lan_environment_rejects_placeholders_simulation_and_unsafe_flags(tmp_path, field, value, code):
    environ = _lan_environ(tmp_path)
    environ[field] = value
    with pytest.raises(PreflightError, match=code):
        validate_lan_environment(environ, "- ${INKTIME_PHOTO_PATH}:/photos:ro")


def test_lan_environment_rejects_same_data_and_photo_path(tmp_path):
    environ = _lan_environ(tmp_path)
    environ["INKTIME_PHOTO_PATH"] = environ["INKTIME_DATA_PATH"]
    with pytest.raises(PreflightError, match="PREFLIGHT-LAN-PATH-003"):
        validate_lan_environment(environ, "- ${INKTIME_PHOTO_PATH}:/photos:ro")


def test_lan_environment_rejects_writable_photo_mount(tmp_path):
    with pytest.raises(PreflightError, match="PREFLIGHT-LAN-MOUNT-001"):
        validate_lan_environment(_lan_environ(tmp_path), "- ${INKTIME_PHOTO_PATH}:/photos")


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
        ({"public_url": "https://inktime.example.net"}, "範例網域"),
        ({"public_url": "https://inktime.your-domain.example"}, "範例網域"),
        ({"public_url": "https://inktime.example.test"}, "範例網域"),
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
