from __future__ import annotations

from pathlib import Path

from inktime.app import bootstrap
from inktime.app.core.preflight import NativeOSAdapter, run_production_preflight
from inktime.app.core.runtime_config import RuntimeConfig
from inktime.app.factory import create_app
from tests.conftest import csrf


def _config(root: Path, *, production: bool) -> RuntimeConfig:
    return RuntimeConfig.from_sources(
        environ={},
        base_dir=root,
        environment="production" if production else "development",
        testing=False,
        development=not production,
        data_dir=root / "data",
        database_path=root / "data/inktime.db",
        photo_dir=root / "photos",
        release_dir=root / "data/releases",
        backup_dir=root / "data/backups",
        cache_dir=root / "data/cache",
        public_url=("https://inktime.home.acme.dev" if production else "http://localhost:8765"),
        cookie_secure=production,
        allow_insecure_http=not production,
        allow_unsafe_network_database=production,
    )


def test_lan_http_setup_cookie_is_returned_and_dashboard_opens(tmp_path):
    app = create_app(_config(tmp_path / "local", production=False))
    try:
        client = app.test_client()
        client.get("/setup")
        response = client.post(
            "/setup",
            data={
                "username": "local-owner",
                "password": "local-owner-password",
                "password_confirmation": "local-owner-password",
                "csrf_token": csrf(client),
            },
        )
        assert response.status_code == 302
        assert "Secure" not in response.headers.get("Set-Cookie", "")
        assert client.get("/dashboard").status_code == 200
        assert "Strict-Transport-Security" not in client.get("/dashboard").headers
    finally:
        app.extensions["inktime_service_container"].close()


def test_production_https_cookie_is_secure_and_hsts_is_https_only(tmp_path, monkeypatch):
    config = _config(tmp_path / "production", production=True)

    class ReadOnlyPhotoMount(NativeOSAdapter):
        def mountinfo(self) -> str:
            return (
                "1 0 0:1 / / ro - ext4 /dev/root ro\n"
                f"2 1 0:2 / {config.photo_dir} ro - ext4 /dev/photos ro"
            )

    monkeypatch.setattr(
        bootstrap,
        "run_production_preflight",
        lambda runtime_config: run_production_preflight(
            runtime_config,
            adapter=ReadOnlyPhotoMount(),
        ),
    )
    app = create_app(config)
    try:
        client = app.test_client()
        https_response = client.get("/setup", base_url="https://inktime.home.acme.dev")
        assert "Secure" in https_response.headers.get("Set-Cookie", "")
        assert https_response.headers["Strict-Transport-Security"].startswith("max-age=31536000")

        http_response = client.get("/setup", base_url="http://inktime.home.acme.dev")
        assert "Strict-Transport-Security" not in http_response.headers
    finally:
        app.extensions["inktime_service_container"].close()
