from __future__ import annotations

from pathlib import Path

import pytest

from inktime.app.core.preflight import (
    PreflightError,
    run_production_preflight,
    validate_lan_environment,
)
from inktime.app.core.runtime_config import RuntimeConfig
from scripts.production_preflight import _lan_prestart_summary


class LocalFilesystem:
    def __init__(self, photo_dir: Path | None = None, *nested: tuple[Path, bool]) -> None:
        self.photo_dir = photo_dir
        self.nested = nested

    def mountinfo(self) -> str:
        lines = ["1 0 0:1 / / ro - ext4 /dev/root ro"]
        if self.photo_dir is not None:
            lines.append(f"2 1 0:2 / {self.photo_dir} ro - ext4 /dev/photos ro")
        for index, (mount_point, read_only) in enumerate(self.nested, start=3):
            mode = "ro" if read_only else "rw"
            lines.append(
                f"{index} 2 0:{index} / {mount_point} {mode} - tmpfs tmpfs {mode}"
            )
        return "\n".join(lines)


class WritablePhotoFilesystem:
    def __init__(self, photo_dir: Path) -> None:
        self.photo_dir = photo_dir

    def mountinfo(self) -> str:
        return (
            "1 0 0:1 / / ro - ext4 /dev/root ro\n"
            f"2 1 0:2 / {self.photo_dir} rw - ext4 /dev/photos rw"
        )


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
    config = _config(tmp_path)
    result = run_production_preflight(config, adapter=LocalFilesystem(config.photo_dir))
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
    config = _config(
        tmp_path,
        public_url=public_url,
        cookie_secure=False,
        allow_insecure_http=True,
        proxy_trust=0,
    )
    result = run_production_preflight(
        config,
        adapter=LocalFilesystem(config.photo_dir),
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
    config = _config(
        tmp_path,
        public_url=public_url,
        cookie_secure=False,
        allow_insecure_http=True,
        proxy_trust=0,
    )
    with pytest.raises(PreflightError, match="PREFLIGHT-LAN-003"):
        run_production_preflight(
            config,
            adapter=LocalFilesystem(config.photo_dir),
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
    validate_lan_environment(
        _lan_environ(tmp_path),
        (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(
            encoding="utf-8"
        ),
    )


def test_lan_prestart_defers_actual_mount_proof_to_container_startup(tmp_path):
    config = _config(
        tmp_path,
        public_url="http://inktime.local:8765",
        cookie_secure=False,
        allow_insecure_http=True,
        proxy_trust=0,
    )

    assert _lan_prestart_summary(config, allow_test_host=False) == {
        "status": "degraded",
        "validation_scope": "prestart-config",
        "transport": "trusted-lan-http",
        "security_state": "degraded",
        "tls_enabled": False,
        "secure_cookie": False,
        "runtime_mount_validation": "deferred-to-container-startup",
    }


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
        validate_lan_environment(environ, "target: /photos\nread_only: true")


def test_lan_environment_rejects_same_data_and_photo_path(tmp_path):
    environ = _lan_environ(tmp_path)
    environ["INKTIME_PHOTO_PATH"] = environ["INKTIME_DATA_PATH"]
    with pytest.raises(PreflightError, match="PREFLIGHT-LAN-PATH-003"):
        validate_lan_environment(environ, "target: /photos\nread_only: true")


def test_lan_environment_rejects_writable_photo_mount(tmp_path):
    with pytest.raises(PreflightError, match="PREFLIGHT-LAN-MOUNT-001"):
        validate_lan_environment(_lan_environ(tmp_path), "- ${INKTIME_PHOTO_PATH}:/photos")


@pytest.mark.parametrize("photo_relative", ["runtime/photos", "runtime", "."])
def test_production_rejects_equal_or_nested_data_and_photo_paths(tmp_path, photo_relative):
    config = _config(tmp_path, photo_dir=tmp_path / photo_relative)
    with pytest.raises(PreflightError, match="DEPLOY-PATH-OVERLAP-001"):
        run_production_preflight(
            config,
            adapter=LocalFilesystem(),
        )


def test_production_rejects_effectively_writable_photo_mount(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(PreflightError, match="DEPLOY-PHOTO-RO-001"):
        run_production_preflight(
            config,
            adapter=WritablePhotoFilesystem(config.photo_dir),
        )


@pytest.mark.parametrize("photo_relative", ["photos", "runtime-old"])
def test_production_accepts_component_distinct_sibling_paths(tmp_path, photo_relative):
    config = _config(tmp_path, photo_dir=tmp_path / photo_relative)
    result = run_production_preflight(
        config,
        adapter=LocalFilesystem(config.photo_dir),
    )
    assert result.healthy


def test_production_rejects_writable_nested_photo_mount(tmp_path):
    config = _config(tmp_path)
    adapter = LocalFilesystem(config.photo_dir, (config.photo_dir / "nested", False))
    with pytest.raises(PreflightError, match="DEPLOY-PHOTO-RO-002"):
        run_production_preflight(config, adapter=adapter)


def test_production_accepts_readonly_nested_photo_mount(tmp_path):
    config = _config(tmp_path)
    adapter = LocalFilesystem(config.photo_dir, (config.photo_dir / "nested", True))
    assert run_production_preflight(config, adapter=adapter).healthy


@pytest.mark.parametrize("sibling", ["photos-archive", "other"])
def test_production_ignores_writable_mounts_outside_photo_tree(tmp_path, sibling):
    config = _config(tmp_path)
    adapter = LocalFilesystem(config.photo_dir, (tmp_path / sibling, False))
    assert run_production_preflight(config, adapter=adapter).healthy


def test_production_requires_exact_photo_mount(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(PreflightError, match="DEPLOY-PHOTO-RO-001"):
        run_production_preflight(config, adapter=LocalFilesystem())


def test_production_decodes_escaped_photo_mount_path(tmp_path):
    config = _config(tmp_path, photo_dir=tmp_path / "photo archive")

    class EscapedPhotoFilesystem:
        def mountinfo(self) -> str:
            escaped = str(config.photo_dir).replace(" ", "\\040")
            return (
                "1 0 0:1 / / ro - ext4 /dev/root ro\n"
                f"2 1 0:2 / {escaped} ro - ext4 /dev/photos ro"
            )

    assert run_production_preflight(config, adapter=EscapedPhotoFilesystem()).healthy


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
    config = _config(tmp_path, **overrides)
    with pytest.raises(PreflightError, match=message):
        run_production_preflight(
            config,
            adapter=LocalFilesystem(config.photo_dir),
        )
