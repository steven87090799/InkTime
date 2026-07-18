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
        "/rendering",
        "/devices",
        "/maintenance",
        "/settings",
        "/diagnostics",
        "/errors",
        "/backups",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "zh-Hant-TW" in response.get_data(as_text=True)


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
