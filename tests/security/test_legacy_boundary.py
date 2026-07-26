from __future__ import annotations

from pathlib import Path
import sys

import pytest

from inktime.app.core.runtime_config import RuntimeConfig
from inktime.app.factory import create_app
from inktime.app.web.templating import AssetCollisionError, assert_no_asset_collisions
from tests.conftest import create_admin, login


def _legacy_app(tmp_path: Path, *, enabled: bool):
    config = RuntimeConfig.from_sources(
        environ={},
        environment="test",
        testing=True,
        development=False,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "db.sqlite",
        photo_dir=tmp_path / "photos",
        release_dir=tmp_path / "releases",
        backup_dir=tmp_path / "backups",
        cache_dir=tmp_path / "cache",
        legacy_enabled=enabled,
        cookie_secure=False,
    )
    return create_app(config)


def test_legacy_is_disabled_by_default_and_heavy_modules_are_not_imported(tmp_path: Path):
    for name in (
        "legacy_server",
        "legacy_analyze_photos",
        "render_daily_photo",
        "render_daily_photo_133c",
        "inktime.app.legacy.blueprint",
    ):
        sys.modules.pop(name, None)
    app = _legacy_app(tmp_path, enabled=False)
    try:
        assert not any(rule.rule.startswith("/legacy") for rule in app.url_map.iter_rules())
        create_admin(app)
        client = app.test_client()
        login(client)
        assert client.get("/legacy/review").status_code == 404
        assert "inktime.app.legacy.blueprint" not in sys.modules
        assert "legacy_server" not in sys.modules
        assert "render_daily_photo" not in sys.modules
    finally:
        app.extensions["inktime_service_container"].close()


def test_enabled_legacy_routes_are_namespaced_admin_only_and_deprecated(tmp_path: Path):
    app = _legacy_app(tmp_path, enabled=True)
    try:
        create_admin(app)
        viewer_id = app.extensions["inktime_auth_repository"].create_user(
            "viewer", "viewer-password", "viewer"
        )
        assert viewer_id
        viewer = app.test_client()
        login(viewer, "viewer", "viewer-password")
        assert viewer.get("/legacy/review").status_code == 403

        admin = app.test_client()
        login(admin)
        response = admin.get("/legacy/review")
        assert response.status_code == 200
        assert "已棄用的相容頁面" in response.get_data(as_text=True)
        assert "禁止新增 Legacy 功能" in response.get_data(as_text=True)
        rules = {rule.rule for rule in app.url_map.iter_rules() if rule.endpoint.startswith("legacy.")}
        assert rules
        assert all(rule.startswith("/legacy/") for rule in rules)
        assert app.test_client().get("/").status_code == 302
    finally:
        app.extensions["inktime_service_container"].close()


def test_legacy_adapter_reads_modern_tables_and_never_writes_photo_scores(tmp_path: Path):
    app = _legacy_app(tmp_path, enabled=True)
    try:
        database = app.extensions["inktime_database"]
        with database.session() as connection:
            connection.executescript(
                """
                CREATE TABLE photo_scores(path TEXT PRIMARY KEY);
                CREATE TRIGGER reject_legacy_insert BEFORE INSERT ON photo_scores
                BEGIN SELECT RAISE(ABORT, 'photo_scores is read only'); END;
                CREATE TRIGGER reject_legacy_update BEFORE UPDATE ON photo_scores
                BEGIN SELECT RAISE(ABORT, 'photo_scores is read only'); END;
                CREATE TRIGGER reject_legacy_delete BEFORE DELETE ON photo_scores
                BEGIN SELECT RAISE(ABORT, 'photo_scores is read only'); END;
                """
            )
        page = app.extensions["inktime_legacy_photo_adapter"].page(page=1, page_size=10)
        assert page.items == ()
        with database.session() as connection:
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='photo_scores'"
            ).fetchone() is not None
    finally:
        app.extensions["inktime_service_container"].close()


def test_template_or_static_collision_fails_explicitly(tmp_path: Path):
    modern = tmp_path / "modern"
    legacy = tmp_path / "legacy"
    modern.mkdir()
    legacy.mkdir()
    (modern / "same.html").write_text("modern", encoding="utf-8")
    (legacy / "same.html").write_text("legacy", encoding="utf-8")
    with pytest.raises(AssetCollisionError, match="衝突"):
        assert_no_asset_collisions(modern, legacy, kind="Template")


def test_legacy_lazy_import_failure_does_not_take_down_modern_app(tmp_path: Path, monkeypatch):
    import inktime.app.factory as factory

    original = factory.import_module

    def fail_legacy(name: str):
        if name == "inktime.app.legacy.blueprint":
            raise ImportError("simulator dependency unavailable")
        return original(name)

    monkeypatch.setattr(factory, "import_module", fail_legacy)
    app = _legacy_app(tmp_path, enabled=True)
    try:
        assert app.extensions["inktime_legacy_available"] is False
        assert app.test_client().get("/health/live").status_code == 200
    finally:
        app.extensions["inktime_service_container"].close()


def test_legacy_registration_collision_fails_factory_explicitly(tmp_path: Path, monkeypatch):
    import inktime.app.factory as factory

    class CollidingLegacy:
        @staticmethod
        def register_legacy(_app):
            raise AssetCollisionError("Modern/Legacy Template 名稱衝突：same.html")

    monkeypatch.setattr(factory, "import_module", lambda _name: CollidingLegacy)
    with pytest.raises(AssetCollisionError, match="名稱衝突"):
        _legacy_app(tmp_path, enabled=True)
