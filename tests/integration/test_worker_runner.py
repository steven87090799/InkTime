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
        name="本地工作", strategy="local", settings={}, created_by="tester",
        budget_limit=0, photo_ids=[photo_id],
    )
    service.start(job_id)
    assert WorkerRunner(app).run_once() == 1
    job = app.extensions["inktime_job_repository"].get(job_id)
    assert job["status"] == "completed"
    assert job["completed_items"] == 1
