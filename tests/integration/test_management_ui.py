from __future__ import annotations

from io import BytesIO

from PIL import Image

from tests.conftest import create_admin, csrf, login
from tests.integration.test_jobs import add_photos


def test_primary_management_pages_render(client, app):
    create_admin(app)
    login(client)
    for path in (
        "/dashboard",
        "/photos",
        "/jobs",
        "/providers",
        "/scoring",
        "/costs",
        "/simulator",
        "/rendering",
        "/devices",
        "/energy",
        "/maintenance",
        "/settings",
        "/diagnostics",
        "/errors",
        "/backups",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "zh-Hant-TW" in response.get_data(as_text=True)


def test_device_energy_dashboard_uses_telemetry_and_audited_measurements(client, app):
    create_admin(app)
    login(client)
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create("客廳 PhotoPainter", panel_profile="gdep073e01_6c")
    status = client.post(
        "/api/device/v1/status",
        json={
            "firmware_version": "2.4.0",
            "battery_percent": 82,
            "battery_percent_estimated": True,
            "battery_voltage": 4.08,
            "usb_power": False,
            "display_updated": True,
            "last_refresh_duration_ms": 25_000,
            "wake_duration_ms": 61_000,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status.status_code == 200

    profile = client.patch(
        f"/api/v1/devices/{device_id}/energy-profile",
        json={
            "battery_capacity_mah": 5000,
            "standby_current_ma": 0.12,
            "active_current_ma": 210,
            "refreshes_per_day": 1,
            "battery_reserve_percent": 10,
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert profile.status_code == 200

    invalid_profile = client.patch(
        f"/api/v1/devices/{device_id}/energy-profile",
        json={"standby_current_ma": -0.1},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert invalid_profile.status_code == 400

    page = client.get(f"/energy?device_id={device_id}&days=30")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "裝置能源儀表板" in body
    assert "82.0%" in body
    assert "25.0 秒" in body
    assert "0.120 mA" in body
    assert "容量／電流模型" in body

    api = client.get(f"/api/v1/devices/{device_id}/energy?days=30")
    assert api.status_code == 200
    assert api.json["summary"]["modeled"]["duration_source"] == "wake_cycle"
    assert api.json["summary"]["sample_count"] == 1
    assert "token_hash" not in api.json["device"]


def test_theme_toggle_is_available_before_and_after_login(client, app):
    setup_page = client.get("/setup").get_data(as_text=True)
    assert 'id="theme-toggle"' in setup_page
    assert "inktime-theme" in setup_page

    create_admin(app)
    login(client)
    dashboard = client.get("/dashboard").get_data(as_text=True)
    assert 'id="theme-toggle"' in dashboard
    assert "深色模式" in dashboard


def test_scoring_rules_and_weights_create_a_new_version(client, app):
    create_admin(app)
    login(client)
    page = client.get("/scoring").get_data(as_text=True)
    assert 'textarea name="rules"' in page
    assert "人物互動或合照，大幅提高評分" in page

    current = app.extensions["inktime_scoring_repository"].current()
    custom_rules = str(current["rules"]) + "\n- 家庭合照再額外提高回憶價值。"
    response = client.post(
        "/api/v1/scoring/profiles",
        json={
            "name": "家庭照片優先",
            "rules": custom_rules,
            "memory_weight": 55,
            "beauty_weight": 15,
            "technical_weight": 10,
            "emotion_weight": 20,
            "favorite_bonus": 8,
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 201
    assert app.extensions["inktime_settings_repository"].get("analysis.scoring_rules") == custom_rules
    assert app.extensions["inktime_scoring_repository"].current()["name"] == "家庭照片優先"


def test_scoring_test_upload_is_normalized_and_not_persisted(client, app, monkeypatch):
    create_admin(app)
    login(client)
    observed = {}

    def fake_analyze(path):
        observed["exists_during_analysis"] = path.exists()
        return {"ranking_score": 88, "analysis": {"caption": "測試照片"}}

    monkeypatch.setattr(
        app.extensions["inktime_scoring_lab_service"], "analyze", fake_analyze
    )
    image = BytesIO()
    Image.new("RGB", (32, 32), "navy").save(image, "JPEG")
    image.seek(0)
    response = client.post(
        "/api/v1/scoring/test",
        data={"photo": (image, "sample.jpg")},
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.json["ranking_score"] == 88
    assert observed["exists_during_analysis"] is True


def test_epaper_simulator_works_without_photo_database_or_model(client, app):
    create_admin(app)
    login(client)
    assert app.extensions["inktime_provider_repository"].list() == []
    image = BytesIO()
    Image.new("RGB", (32, 48), (42, 110, 180)).save(image, "PNG")
    image.seek(0)

    response = client.post(
        "/api/v1/rendering/simulate",
        data={
            "photo": (image, "standalone.png"),
            "profile": "safe_4c",
            "dither": "none",
            "fit": "contain",
            "strength": "0",
            "color_distance": "oklab",
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.headers["X-InkTime-Model"] == "disabled"
    assert response.headers["X-InkTime-Canvas"] == "480x800"
    assert response.headers["X-InkTime-Payload-Bytes"] == "96000"
    rendered = Image.open(BytesIO(response.data))
    assert rendered.size == (480, 800)
    assert set(rendered.getdata()).issubset(
        {(0, 0, 0), (255, 255, 255), (220, 30, 30), (245, 190, 25)}
    )
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0


def test_epaper_simulator_rejects_unknown_profile(client, app):
    create_admin(app)
    login(client)
    image = BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, "PNG")
    image.seek(0)
    response = client.post(
        "/api/v1/rendering/simulate",
        data={"photo": (image, "sample.png"), "profile": "not-a-panel"},
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.json["error_code"] == "RENDER-004"


def test_backup_is_integrity_checked_and_downloadable(client, app):
    create_admin(app)
    login(client)
    service = app.extensions["inktime_backup_service"]
    archive = service.create()
    manifest = service.validate(archive)
    assert "原始照片" in manifest["excludes"]
    response = client.get(f"/api/v1/backups/{archive.name}")
    assert response.status_code == 200
    assert response.mimetype == "application/zip"


def test_diagnostic_bundle_excludes_sensitive_categories(client, app):
    create_admin(app)
    login(client)
    response = client.get("/api/v1/diagnostics/bundle")
    assert response.status_code == 200
    body = response.get_data()
    for forbidden in (b"api_key", b"cookie", b"gps_lat", b"session.key"):
        assert forbidden not in body.lower()


def test_photo_manual_edit_is_audited(client, app):
    create_admin(app)
    login(client)
    photo_id = add_photos(app, 1)[0]
    response = client.patch(
        f"/api/v1/photos/{photo_id}",
        json={
            "favorite": True,
            "captured_at": "2026-07-17T10:00:00",
            "types": ["家庭"],
            "side_caption": "值得收藏的一天",
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT favorite,captured_at FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        event = connection.execute("SELECT event FROM photo_events WHERE photo_id=?", (photo_id,)).fetchone()
    assert tuple(photo) == (1, "2026-07-17T10:00:00")
    assert event["event"] == "manual_update"
