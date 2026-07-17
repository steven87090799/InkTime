from __future__ import annotations

from PIL import Image

from inktime.app.domain.photos import PhotoPreprocessor
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.workers.runner import WorkerRunner
from inktime.app.workers.scanner import PhotoScanner


def test_production_runner_completes_local_job_without_provider(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    Image.new("RGB", (200, 150), "blue").save(root / "a.jpg")
    photos = PhotoRepository(app.extensions["inktime_database"])
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan("照片", root)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = connection.execute("SELECT id FROM photos").fetchone()[0]
    service = app.extensions["inktime_job_service"]
    job_id = service.create_analysis_job(
        name="本地工作",
        strategy="local",
        settings={},
        created_by="tester",
        budget_limit=0,
        photo_ids=[photo_id],
    )
    service.start(job_id)
    assert WorkerRunner(app).run_once() == 1
    job = app.extensions["inktime_job_repository"].get(job_id)
    assert job["status"] == "completed"
    assert job["completed_items"] == 1


def test_scan_requested_by_ui_runs_as_background_job(client, app, tmp_path):
    from tests.conftest import create_admin, csrf, login

    root = tmp_path / "ui-photos"
    root.mkdir()
    Image.new("RGB", (100, 80), "green").save(root / "new.jpg")
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/maintenance/scan",
        json={"library_name": "NAS", "root_path": str(root), "build_thumbnails": True},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 202
    job_id = response.get_json()["id"]
    assert app.extensions["inktime_job_repository"].get(job_id)["status"] == "running"
    WorkerRunner(app).run_once()
    assert app.extensions["inktime_job_repository"].get(job_id)["status"] == "completed"
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1


def test_release_requested_by_ui_runs_as_background_job(client, app, monkeypatch):
    from tests.conftest import create_admin, csrf, login

    published = []

    def publish(photo_ids, created_by):
        published.append((photo_ids, created_by))
        return {"release_id": "test-release"}

    monkeypatch.setattr(app.extensions["inktime_render_service"], "publish", publish)
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/releases",
        json={"photo_ids": []},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 202
    job_id = response.get_json()["id"]
    WorkerRunner(app).run_once()
    assert app.extensions["inktime_job_repository"].get(job_id)["status"] == "completed"
    assert published and published[0][0] == []
