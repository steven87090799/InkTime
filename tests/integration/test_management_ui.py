from __future__ import annotations

from tests.conftest import create_admin, login


def test_primary_management_pages_render(client, app):
    create_admin(app)
    login(client)
    for path in ("/dashboard", "/photos", "/jobs", "/providers", "/costs", "/devices", "/settings", "/diagnostics", "/errors", "/backups"):
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
