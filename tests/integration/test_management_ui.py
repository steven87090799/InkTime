from __future__ import annotations

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
