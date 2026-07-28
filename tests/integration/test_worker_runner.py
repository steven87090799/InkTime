from __future__ import annotations

import json

from PIL import Image

from inktime.app.domain.analysis.plan import fingerprint
from inktime.app.domain.photos import PhotoPreprocessor
from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.workers.runner import WorkerRunner
from inktime.app.workers.scanner import PhotoScanner
from tests.unit.test_analysis_schema import valid_result


class FrozenPlanProvider(VisionProvider):
    name = "Frozen Plan Provider"
    provider_id = "frozen-provider"

    def __init__(self):
        self.calls: list[dict] = []

    def analyze(self, **kwargs):
        self.calls.append(kwargs)
        return ProviderResponse(json.dumps(valid_result(), ensure_ascii=False), Usage(100, 20, 0))

    def repair_json(self, **_kwargs):
        raise AssertionError("valid result must not require repair")

    def submit_batch(self, requests, completion_window="24h"):
        return "batch"

    def poll_batch(self, batch_id):
        return {"status": "completed"}

    def cancel_batch(self, batch_id):
        return {"status": "cancelled"}

    def estimate_cost(self, model, usage):
        return 0.0

    def validate_config(self):
        return True, "ok"


def test_production_runner_completes_local_job_without_provider(app, tmp_path, monkeypatch):
    root = tmp_path / "photos"
    root.mkdir()
    Image.new("RGB", (200, 150), "blue").save(root / "a.jpg")
    photos = PhotoRepository(app.extensions["inktime_database"])
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan("照片", root)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = connection.execute("SELECT id FROM photos").fetchone()[0]
    service = app.extensions["inktime_job_service"]
    monkeypatch.setattr(
        app.extensions["inktime_provider_service"],
        "build_router",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("local job must not load Provider secrets")),
    )
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


def test_cloud_job_with_an_empty_frozen_route_fails_without_discovering_provider(app, tmp_path):
    app.extensions["inktime_settings_repository"].update(
        "analysis.execution_mode", "automatic_ai", changed_by="test", source_ip="127.0.0.1"
    )
    root = tmp_path / "photos"
    root.mkdir()
    Image.effect_noise((900, 600), 90).convert("RGB").save(root / "a.jpg")
    photos = PhotoRepository(app.extensions["inktime_database"])
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan("照片", root)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
        connection.execute("UPDATE photos SET eligible=1,exclusion_status='eligible' WHERE id=?", (photo_id,))
    analysis = app.extensions["inktime_analysis_service"]
    plan = analysis.build_plan(
        strategy="high_quality",
        provider_route=[],
        scoring_profile=dict(app.extensions["inktime_scoring_repository"].current()),
    )
    jobs = app.extensions["inktime_job_service"]
    job_id = jobs.create_analysis_job(
        name="empty frozen route",
        strategy="high_quality",
        settings={},
        created_by="tester",
        budget_limit=None,
        photo_ids=[photo_id],
        analysis_fingerprint=fingerprint(plan),
        analysis_spec=plan,
    )
    jobs.start(job_id)
    assert WorkerRunner(app).run_once() == 1
    item = app.extensions["inktime_job_repository"].list_items(job_id)[0]
    assert item["status"] == "failed"
    with app.extensions["inktime_database"].session() as connection:
        error = connection.execute(
            "SELECT error_code,message FROM job_errors WHERE job_item_id=? ORDER BY id DESC LIMIT 1",
            (item["id"],),
        ).fetchone()
    assert error["error_code"] == "VLM-008"
    assert "尚未設定可用 Provider" in error["message"]


def test_runner_uses_the_frozen_job_plan_after_settings_change(app, tmp_path, monkeypatch):
    root = tmp_path / "photos"
    root.mkdir()
    Image.effect_noise((900, 600), 100).convert("RGB").save(root / "a.jpg")
    photos = PhotoRepository(app.extensions["inktime_database"])
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan("照片", root)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id=?",
            (photo_id,),
        )
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.ai_mode", "eligible", changed_by="test", source_ip="127.0.0.1")
    analysis = app.extensions["inktime_analysis_service"]
    route = [{"provider_id": "frozen-provider", "display_name": "Frozen", "priority": 1, "config_revision": "v1"}]
    plan = analysis.build_plan(
        strategy="high_quality",
        provider_route=route,
        scoring_profile=dict(app.extensions["inktime_scoring_repository"].current()),
    )
    provider = FrozenPlanProvider()
    routed = []

    def build_router(snapshot, *, scoring_rules=None):
        routed.append((snapshot, scoring_rules))
        return provider

    monkeypatch.setattr(app.extensions["inktime_provider_service"], "build_router", build_router)
    job_service = app.extensions["inktime_job_service"]
    job_id = job_service.create_analysis_job(
        name="frozen plan",
        strategy="high_quality",
        settings={},
        created_by="tester",
        budget_limit=None,
        photo_ids=[photo_id],
        analysis_fingerprint=fingerprint(plan),
        analysis_spec=plan,
    )
    settings.update("analysis.ai_mode", "off", changed_by="test", source_ip="127.0.0.1")
    settings.update("model.high_model", "changed-after-queue", changed_by="test", source_ip="127.0.0.1")
    job_service.start(job_id)

    assert WorkerRunner(app).run_once() == 1
    assert app.extensions["inktime_job_repository"].get(job_id)["status"] == "completed"
    assert routed == [(route, plan["scoring_rules"])]
    assert provider.calls and provider.calls[0]["model"] == plan["high_model"]
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT analysis_fingerprint,analysis_spec_json FROM photo_analysis WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (photo_id,),
        ).fetchone()
    assert row["analysis_fingerprint"] == fingerprint(plan)
    assert json.loads(row["analysis_spec_json"]) == plan


def test_drain_worker_exits_after_current_queue(app):
    repository = app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(kind="cleanup", name="快取清理", settings={}, created_by="tester")
    app.extensions["inktime_job_service"].start(job_id)
    assert WorkerRunner(app).run_drain() == 1
    assert repository.get(job_id)["status"] == "completed"


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


def test_virtual_display_inbox_scans_and_publishes_without_provider(client, app, tmp_path):
    from tests.conftest import create_admin, csrf, login

    root = tmp_path / "simulation-photos"
    root.mkdir()
    Image.effect_noise((480, 800), 100).convert("RGB").save(root / "receiver-test.png")
    app.config["INKTIME_PHOTO_DIR"] = root
    create_admin(app)
    login(client)

    response = client.post(
        "/api/v1/maintenance/virtual-display",
        headers={"X-CSRF-Token": csrf(client)},
    )

    assert response.status_code == 202
    assert response.json["receiver_url"] == "/virtual-display"
    job_id = response.json["id"]
    assert WorkerRunner(app).run_once() == 1
    job = app.extensions["inktime_job_repository"].get(job_id)
    assert job["status"] == "completed"
    profile_key = str(app.extensions["inktime_settings_repository"].get("render.profile"))
    manifest = client.get(f"/api/v1/virtual-display/manifest?profile={profile_key}")
    assert manifest.status_code == 200
    assert manifest.json["files"][0]["source_photo_id"]
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0
