from __future__ import annotations

from io import BytesIO
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import time

from PIL import Image
import pytest

from tests.conftest import create_admin, csrf, login
from inktime.app.workers.runner import WorkerRunner
from inktime.app.domain.rendering import DeviceTestReleaseStore
from inktime.app.workers.process_boundary import ProcessCallError, ProcessCallTimeout


def _photo_upload(color: str = "#5079a8"):
    output = BytesIO()
    Image.new("RGB", (160, 240), color).save(output, "PNG")
    output.seek(0)
    return output, "person.png"


def _large_photo_upload():
    output = BytesIO()
    Image.new("RGB", (3500, 3500), "#5079a8").save(output, "PNG")
    output.seek(0)
    return output, "large.png"


def _hang_library_preview_child():
    time.sleep(5)


def _library_photo(app, root: Path, photo_id: str = "preview-photo") -> str:
    root.mkdir(exist_ok=True)
    Image.new("RGB", (640, 480), "#5079a8").save(root / f"{photo_id}.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("Preview 測試", root)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(
                id,library_id,relative_path,sha256,width,height,status,eligible,
                lifecycle_status,crop_focus_x,crop_focus_y,e6_score,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,'analyzed',1,'active',.5,.5,80,datetime('now'),datetime('now'))
            """,
            (photo_id, library_id, f"{photo_id}.jpg", "a" * 64, 640, 480),
        )
    photos.save_analysis(
        photo_id,
        None,
        "local",
        "local",
        "test",
        {
            "schema_version": 1,
            "caption": "Preview 測試",
            "types": ["其他"],
            "memory_score": 80,
            "beauty_score": 80,
            "technical_quality_score": 80,
            "emotion_score": 80,
            "side_caption": "Preview 必須由背景工作產生。",
            "should_keep": True,
            "sensitive": False,
            "reason": "test",
        },
        "{}",
        ranking_score=80,
    )
    return photo_id


def _queued_test_release(client, app, *, save_preset: bool = False):
    repository = app.extensions["inktime_device_repository"]
    device_id, _token = repository.create(
        "Test Release 隔離", panel_profile="gdep073e01_6c"
    )
    response = client.post(
        "/api/v1/rendering/test-release",
        data={
            "photo": _photo_upload(),
            "device_id": device_id,
            "profile": "gdep073e01_6c",
            "preset": "photo_balanced",
            "fit": "cover",
            "options": '{"dither":"nearest"}',
            "palette": '{"mode":"default"}',
            "delivery": "next_wake",
            "one_time": "true",
            "restore_formal": "true",
            "save_preset": "true" if save_preset else "false",
            "preset_label": "Idempotent Preset",
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]
    return device_id, job_id


def _claimed_test_release(client, app, *, save_preset: bool = False):
    device_id, job_id = _queued_test_release(
        client, app, save_preset=save_preset
    )
    jobs = app.extensions["inktime_job_repository"]
    item = dict(jobs.claim(job_id, "test-worker", 1)[0])
    settings = json.loads(str(jobs.get(job_id)["settings_json"]))
    context = {
        "job_id": job_id,
        "item_id": str(item["id"]),
        "worker_id": "test-worker",
        "idempotency_key": str(item["idempotency_key"]),
    }
    return device_id, settings, context


def test_browser_canvas_cannot_be_published_as_test_release(client, app):
    create_admin(app)
    login(client)
    device_id, _ = app.extensions["inktime_device_repository"].create(
        "六色測試", panel_profile="gdep073e01_6c"
    )
    response = client.post(
        "/api/v1/rendering/test-release",
        data={"device_id": device_id, "canvas_data": "data:image/png;base64,unsafe"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 400
    assert "Browser Canvas 不可直接發布" in response.get_json()["message"]


def test_viewer_cannot_create_test_release(client, app):
    app.extensions["inktime_auth_repository"].create_user(
        "viewer", "very-safe-passphrase", role="viewer"
    )
    login(client, username="viewer")
    response = client.post(
        "/api/v1/rendering/test-release",
        data={"photo": _photo_upload()},
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 403


def test_preview_jobs_have_per_user_limit(client, app):
    create_admin(app)
    login(client)
    headers = {"X-CSRF-Token": csrf(client)}
    for _index in range(2):
        response = client.post(
            "/api/v1/rendering/compare",
            data={
                "photo": _photo_upload(),
                "profile": "gdep073e01_6c",
                "preset": "photo_balanced",
                "fit": "cover",
                "options": '{"dither":"nearest"}',
                "palette": '{"mode":"default"}',
            },
            headers=headers,
            content_type="multipart/form-data",
        )
        assert response.status_code == 202
    inputs = set(app.extensions["inktime_render_workload_service"].input_root.iterdir())
    limited = client.post(
        "/api/v1/rendering/compare",
        data={
            "photo": _photo_upload(),
            "profile": "gdep073e01_6c",
            "preset": "photo_balanced",
            "fit": "cover",
            "options": '{"dither":"nearest"}',
            "palette": '{"mode":"default"}',
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert limited.status_code == 429
    assert set(app.extensions["inktime_render_workload_service"].input_root.iterdir()) == inputs


def test_preview_job_creation_failure_cleans_only_new_input(client, app, monkeypatch):
    create_admin(app)
    login(client)
    workload = app.extensions["inktime_render_workload_service"]
    before = set(workload.input_root.iterdir())
    monkeypatch.setattr(
        app.extensions["inktime_job_repository"],
        "create_maintenance_with_capacity",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("create failed")),
    )
    with pytest.raises(RuntimeError):
        client.post(
            "/api/v1/rendering/compare",
            data={
                "photo": _photo_upload(),
                "profile": "gdep073e01_6c",
                "preset": "photo_balanced",
                "fit": "cover",
                "options": '{"dither":"nearest"}',
                "palette": '{"mode":"default"}',
            },
            headers={"X-CSRF-Token": csrf(client)},
            content_type="multipart/form-data",
        )
    assert set(workload.input_root.iterdir()) == before
    assert app.extensions["inktime_job_repository"].list() == []


def test_input_save_failure_does_not_leave_pending_job(client, app, monkeypatch):
    create_admin(app)
    login(client)
    workload = app.extensions["inktime_render_workload_service"]
    monkeypatch.setattr(
        workload,
        "save_upload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("IMG-002 儲存失敗")
        ),
    )
    response = client.post(
        "/api/v1/rendering/compare",
        data={
            "photo": _photo_upload(),
            "profile": "gdep073e01_6c",
            "preset": "photo_balanced",
            "fit": "cover",
            "options": '{"dither":"nearest"}',
            "palette": '{"mode":"default"}',
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert app.extensions["inktime_job_repository"].list() == []


def test_library_preview_cache_miss_never_renders_in_request_thread(
    client, app, tmp_path, monkeypatch
):
    create_admin(app)
    login(client)
    photo_id = _library_photo(app, tmp_path / "preview-library")
    service = app.extensions["inktime_render_service"]
    monkeypatch.setattr(
        service,
        "render_photo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("parent process must not render a library preview")
        ),
    )
    response = client.get(f"/api/v1/rendering/preview/{photo_id}")
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]
    WorkerRunner(app).run_once()
    item = app.extensions["inktime_job_repository"].list_items(job_id)[0]
    result = json.loads(str(item["result_json"]))
    assert str(item["status"]) == "completed"
    assert result["stage"] == "preview_completed"
    assert result["cache_hit"] is False
    assert result["preview_url"].endswith("/preview.png")
    assert list(
        app.extensions["inktime_render_workload_service"].prepared_root.iterdir()
    ) == []


def test_quantized_preview_preserves_resolved_auto_dither_in_background(
    client, app, tmp_path, monkeypatch
):
    create_admin(app)
    login(client)
    photo_id = _library_photo(app, tmp_path / "preview-auto-dither", "high-risk")
    settings_repository = app.extensions["inktime_settings_repository"]
    settings_repository.update(
        "render.auto_photo_smooth_enabled", True,
        changed_by="test", source_ip="127.0.0.1",
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE photos
            SET brightness=40,contrast=5,underexposed_ratio=.7,e6_score=20,
                e6_contrast_score=20,e6_subject_score=20
            WHERE id=?
            """,
            (photo_id,),
        )

    response = client.get(
        f"/api/v1/rendering/preview/{photo_id}?quantized=1&profile=safe_4c"
    )
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]
    jobs = app.extensions["inktime_job_repository"]
    queued = json.loads(str(jobs.get(job_id)["settings_json"]))
    arguments = queued["arguments"]
    assert arguments["requested_dither"] is None
    assert arguments["effective_dither"] == "photo_smooth"
    assert arguments["override_source"] == "auto_photo_smooth"

    workload = app.extensions["inktime_render_workload_service"]
    real_call = workload.process_boundary.call
    captured: dict = {}

    def capture_child_call(function, **options):
        captured.update(options["kwargs"]["settings"]["arguments"])
        return real_call(function, **options)

    monkeypatch.setattr(workload.process_boundary, "call", capture_child_call)
    WorkerRunner(app).run_once()
    item = jobs.list_items(job_id)[0]
    result = json.loads(str(item["result_json"]))
    assert captured["effective_dither"] == "photo_smooth"
    assert captured["requested_dither"] is None
    assert result["effective_dither"] == "photo_smooth"
    assert result["override_source"] == "auto_photo_smooth"

    cached = client.get(
        f"/api/v1/rendering/preview/{photo_id}?quantized=1&profile=safe_4c"
    )
    assert cached.status_code == 200
    assert cached.headers["X-InkTime-Effective-Dither"] == "photo_smooth"
    assert cached.headers["X-InkTime-Dither-Override-Source"] == "auto_photo_smooth"


def test_quantized_preview_treats_explicit_dither_as_an_override(client, app, tmp_path):
    create_admin(app)
    login(client)
    photo_id = _library_photo(app, tmp_path / "preview-request-dither", "override")
    settings_repository = app.extensions["inktime_settings_repository"]
    settings_repository.update(
        "render.auto_photo_smooth_enabled", True,
        changed_by="test", source_ip="127.0.0.1",
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE photos
            SET brightness=40,contrast=5,underexposed_ratio=.7,e6_score=20,
                e6_contrast_score=20,e6_subject_score=20
            WHERE id=?
            """,
            (photo_id,),
        )

    response = client.get(
        f"/api/v1/rendering/preview/{photo_id}?quantized=1&profile=safe_4c&dither=gooddisplay"
    )
    assert response.status_code == 202
    queued = json.loads(
        str(app.extensions["inktime_job_repository"].get(response.get_json()["job_id"])["settings_json"])
    )
    arguments = queued["arguments"]
    assert arguments["requested_dither"] == "gooddisplay"
    assert arguments["effective_dither"] == "gooddisplay"
    assert arguments["override_source"] == "request_override"


def test_library_preview_timeout_retries_without_committing_artifacts(
    client, app, tmp_path, monkeypatch
):
    create_admin(app)
    login(client)
    photo_id = _library_photo(app, tmp_path / "preview-timeout", "timeout-photo")
    response = client.get(f"/api/v1/rendering/preview/{photo_id}")
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]
    jobs = app.extensions["inktime_job_repository"]
    with app.extensions["inktime_database"].session() as connection:
        settings = json.loads(
            str(connection.execute(
                "SELECT settings_json FROM jobs WHERE id=?", (job_id,)
            ).fetchone()["settings_json"])
        )
        settings["max_retries"] = 1
        connection.execute(
            "UPDATE jobs SET settings_json=? WHERE id=?",
            (json.dumps(settings, ensure_ascii=False), job_id),
        )
    workload = app.extensions["inktime_render_workload_service"]
    render_cache = app.extensions["inktime_render_cache"]
    process_boundary = workload.process_boundary
    real_call = process_boundary.call

    def timeout_in_spawn_child(_function, **options):
        return real_call(
            _hang_library_preview_child,
            timeout_seconds=0.1,
            kwargs={},
            cancel_requested=options.get("cancel_requested"),
            process_name="inktime-library-preview-timeout-child",
        )

    monkeypatch.setattr(
        process_boundary,
        "call",
        timeout_in_spawn_child,
    )

    WorkerRunner(app).run_once()

    job = jobs.get(job_id)
    item = jobs.list_items(job_id)[0]
    assert str(job["status"]) == "completed_with_errors"
    assert str(item["status"]) == "failed"
    assert str(item["error_code"]) == ProcessCallTimeout.code
    assert list(render_cache.root.glob("*.png")) == []
    assert list(workload.result_root.iterdir()) == []
    assert list(workload.prepared_root.iterdir()) == []
    assert process_boundary.observability()["active"] == 0
    assert process_boundary.observability()["timeout"] == 1
    assert process_boundary.observability()["terminated"] == 1
    assert not [
        child
        for child in multiprocessing.active_children()
        if child.name == "inktime-library-preview-timeout-child"
    ]


def test_library_preview_lease_loss_discards_prepared_cache_and_result(
    client, app, tmp_path, monkeypatch
):
    create_admin(app)
    login(client)
    photo_id = _library_photo(app, tmp_path / "preview-lease", "lease-photo")
    response = client.get(f"/api/v1/rendering/preview/{photo_id}")
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]
    jobs = app.extensions["inktime_job_repository"]
    workload = app.extensions["inktime_render_workload_service"]
    render_cache = app.extensions["inktime_render_cache"]

    def finish_after_lease_loss(_function, *, kwargs, **_options):
        prepared = Path(kwargs["prepared_path"])
        Image.new("RGB", (480, 800), "white").save(prepared / "preview.png")
        with app.extensions["inktime_database"].session() as connection:
            connection.execute(
                "UPDATE job_items SET lease_until='2000-01-01T00:00:00+00:00' "
                "WHERE job_id=?",
                (job_id,),
            )
        return {"stage": "preview_completed"}

    monkeypatch.setattr(workload.process_boundary, "call", finish_after_lease_loss)
    WorkerRunner(app).run_once()

    item = jobs.list_items(job_id)[0]
    assert str(item["status"]) == "pending"
    assert str(item["error_code"]) == ProcessCallError.code
    assert list(render_cache.root.glob("*.png")) == []
    assert list(workload.result_root.iterdir()) == []
    assert list(workload.prepared_root.iterdir()) == []


def test_library_preview_cache_hit_returns_png_without_render(
    client, app, tmp_path, monkeypatch
):
    create_admin(app)
    login(client)
    photo_id = _library_photo(app, tmp_path / "preview-cache", "cache-photo")
    service = app.extensions["inktime_render_service"]
    fingerprint = service.preview_fingerprint(photo_id)
    app.extensions["inktime_render_cache"].put(
        fingerprint, Image.new("RGB", (480, 800), "white")
    )
    monkeypatch.setattr(
        service,
        "render_photo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cache hit must not render")
        ),
    )
    response = client.get(f"/api/v1/rendering/preview/{photo_id}")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.headers["X-InkTime-Renderer-Cache"] == "hit"


def test_preview_fingerprint_tracks_all_render_versions(app, tmp_path, monkeypatch):
    primary = _library_photo(app, tmp_path / "fingerprint", "fingerprint-primary")
    secondary = _library_photo(
        app, tmp_path / "fingerprint", "fingerprint-secondary"
    )
    service = app.extensions["inktime_render_service"]
    settings = app.extensions["inktime_settings_repository"]

    def key(**kwargs):
        return app.extensions["inktime_render_cache"].fingerprint(
            service.preview_fingerprint(
                primary, secondary_photo_id=secondary, **kwargs
            )
        )

    baseline = key()
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photo_analysis SET side_caption='更新後 Caption' WHERE photo_id=?",
            (primary,),
        )
    caption_changed = key()
    assert caption_changed != baseline

    settings.update(
        "analysis.advanced_caption_enabled", True, changed_by="test", source_ip="local"
    )
    settings.update(
        "analysis.copy_default_style", "warm", changed_by="test", source_ip="local"
    )
    style_changed = key()
    assert style_changed != caption_changed

    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET manual_orientation_rotation_cw=90,"
            "manual_orientation_updated_at=datetime('now') WHERE id=?",
            (primary,),
        )
    manual_orientation_changed = key()
    assert manual_orientation_changed != style_changed

    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET manual_orientation_rotation_cw=180,"
            "manual_orientation_updated_at=datetime('now') WHERE id=?",
            (secondary,),
        )
    secondary_orientation_changed = key()
    assert secondary_orientation_changed != manual_orientation_changed

    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE photos SET crop_focus_x=.2,crop_focus_y=.7,
                crop_subject_left=.1,crop_subject_top=.2,
                crop_subject_right=.8,crop_subject_bottom=.9
            WHERE id=?
            """,
            (primary,),
        )
    crop_changed = key()
    assert crop_changed != secondary_orientation_changed

    font_manager = app.extensions["inktime_font_manager"]
    uploaded = font_manager.root / "fingerprint.ttf"
    shutil.copyfile(font_manager.resolve("builtin:iansui"), uploaded)
    settings.update(
        "render.font_path",
        "uploaded:fingerprint.ttf",
        changed_by="test",
        source_ip="local",
    )
    font_before = key()
    payload = bytearray(uploaded.read_bytes())
    payload[-1] ^= 1
    uploaded.write_bytes(payload)
    os.utime(uploaded, None)
    font_after = key()
    assert font_after != font_before

    monkeypatch.setattr(
        service.weather, "snapshot_fingerprint", lambda: {"snapshot": "weather-a"}
    )
    weather_before = key(layout="weather_sensor")
    monkeypatch.setattr(
        service.weather, "snapshot_fingerprint", lambda: {"snapshot": "weather-b"}
    )
    weather_after = key(layout="weather_sensor")
    assert weather_after != weather_before

    unrelated_before = key()
    settings.update("backup.retention", 15, changed_by="test", source_ip="local")
    assert key() == unrelated_before


def test_simulator_upload_does_not_encode_in_web_thread(client, app, monkeypatch):
    create_admin(app)
    login(client)
    from inktime.app.services import render_workloads

    monkeypatch.setattr(
        render_workloads,
        "encode_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Web request must not encode")
        ),
    )
    response = client.post(
        "/api/v1/rendering/simulate",
        data={"photo": _photo_upload(), "profile": "safe_4c", "dither": "nearest"},
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202


def test_oversized_simulator_preview_returns_background_job(client, app):
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/rendering/simulate",
        data={
            "photo": _large_photo_upload(),
            "profile": "safe_4c",
            "dither": "nearest",
            "fit": "contain",
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    created = response.get_json()
    WorkerRunner(app).run_once()
    status = client.get(created["status_url"]).get_json()
    assert status["status"] == "completed"
    preview = client.get(status["result"]["preview"])
    assert preview.status_code == 200
    assert preview.mimetype == "image/png"


def test_ab_preview_is_server_rendered_and_reports_palette_statistics(client, app):
    create_admin(app)
    login(client)
    page = client.get("/simulator")
    assert page.status_code == 200
    assert "原圖" in page.get_data(as_text=True)
    assert "舊微雪算法" in page.get_data(as_text=True)
    assert "新算法" in page.get_data(as_text=True)
    response = client.post(
        "/api/v1/rendering/compare",
        data={
            "photo": _photo_upload("#c59d78"),
            "profile": "gdep073e01_6c",
            "preset": "photo_balanced",
            "fit": "cover",
            "options": '{"dither":"nearest"}',
            "palette": '{"mode":"default"}',
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    created = response.get_json()
    WorkerRunner(app).run_once()
    status = client.get(created["status_url"])
    assert status.status_code == 200
    assert status.get_json()["status"] == "completed"
    body = status.get_json()["result"]
    assert body["publish_source"] == "server_original_upload_only"
    assert body["payload_bytes"] == 192_000
    assert len(body["palette"]) == 6
    assert sum(item["pixels"] for item in body["palette"]) == 480 * 800

    repeated = client.post(
        "/api/v1/rendering/compare",
        data={
            "photo": _photo_upload("#c59d78"),
            "profile": "gdep073e01_6c",
            "preset": "photo_balanced",
            "fit": "cover",
            "options": '{"dither":"nearest"}',
            "palette": '{"mode":"default"}',
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert repeated.status_code == 202
    WorkerRunner(app).run_once()
    assert app.extensions["inktime_render_workload_service"].observability()[
        "compare_cache_hit"
    ] == 1


def test_job_status_and_background_results_are_owner_scoped(app):
    auth = app.extensions["inktime_auth_repository"]
    auth.create_user("owner", "owner-passphrase-long", role="viewer")
    auth.create_user("other", "other-passphrase-long", role="viewer")
    create_admin(app)
    owner = app.test_client()
    other = app.test_client()
    administrator = app.test_client()
    anonymous = app.test_client()
    login(owner, username="owner", password="owner-passphrase-long")
    login(other, username="other", password="other-passphrase-long")
    login(administrator)

    response = owner.post(
        "/api/v1/rendering/compare",
        data={
            "photo": _photo_upload(),
            "profile": "gdep073e01_6c",
            "preset": "photo_balanced",
            "fit": "cover",
            "options": '{"dither":"nearest"}',
            "palette": '{"mode":"default"}',
        },
        headers={"X-CSRF-Token": csrf(owner)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    created = response.get_json()
    WorkerRunner(app).run_once()
    owner_status = owner.get(created["status_url"])
    assert owner_status.status_code == 200
    result_url = owner_status.get_json()["result"]["new"]
    assert owner.get(result_url).status_code == 200
    assert other.get(created["status_url"]).status_code == 404
    assert other.get(result_url).status_code == 404
    assert administrator.get(created["status_url"]).status_code == 200
    assert administrator.get(result_url).status_code == 200
    assert anonymous.get(created["status_url"]).status_code == 401
    assert anonymous.get(result_url).status_code == 401
    assert administrator.get(
        "/api/v1/rendering/background-results/" + "0" * 32 + "/preview.png"
    ).status_code == 404


def test_test_release_is_one_time_and_does_not_overwrite_formal_schedule(client, app):
    create_admin(app)
    login(client)
    repository = app.extensions["inktime_device_repository"]
    device_id, token = repository.create(
        "六色測試",
        schedule="07:30",
        panel_profile="gdep073e01_6c",
    )
    publisher = app.extensions["inktime_release_publisher"]
    formal = publisher.publish(
        [("formal", Image.new("RGB", (480, 800), "white"))],
        profile_key="gdep073e01_6c",
    )
    pointer = app.config["INKTIME_RELEASE_DIR"] / "latest.gdep073e01_6c"
    response = client.post(
        "/api/v1/rendering/test-release",
        data={
            "photo": _photo_upload(),
            "device_id": device_id,
            "profile": "gdep073e01_6c",
            "preset": "photo_balanced",
            "fit": "cover",
            "options": '{"dither":"nearest"}',
            "palette": '{"mode":"default"}',
            "delivery": "next_wake",
            "one_time": "true",
            "restore_formal": "true",
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    created = response.get_json()
    WorkerRunner(app).run_once()
    status = client.get(created["status_url"])
    assert status.status_code == 200
    assert status.get_json()["status"] == "completed"
    test_release = status.get_json()["result"]
    assert test_release["formal_schedule_overwritten"] is False
    assert pointer.read_text() == formal["release_id"]
    assert repository.get(device_id)["schedule"] == "07:30"

    headers = {"Authorization": f"Bearer {token}"}
    assigned = client.get("/api/device/v1/releases/latest", headers=headers).get_json()
    assert assigned["release_id"] == test_release["release_id"]
    assert assigned["release_kind"] == "device_test"
    downloaded = client.get(
        assigned["download_base_url"] + assigned["files"][0]["name"], headers=headers
    )
    assert downloaded.status_code == 200
    downloaded.close()
    still_assigned = client.get("/api/device/v1/releases/latest", headers=headers).get_json()
    assert still_assigned["release_id"] == test_release["release_id"]
    acknowledged = client.post(
        "/api/device/v1/status",
        headers=headers,
        json={
            "release_id": test_release["release_id"],
            "payload_sha256_verified": True,
            "display_updated": True,
            "render_profile": "gdep073e01_6c",
            "error_code": "",
        },
    )
    assert acknowledged.status_code == 200
    restored = client.get("/api/device/v1/releases/latest", headers=headers).get_json()
    assert restored["release_id"] == formal["release_id"]


def test_test_release_render_timeout_has_no_publish_assign_or_preset(
    client, app, monkeypatch
):
    create_admin(app)
    login(client)
    device_id, settings, context = _claimed_test_release(
        client, app, save_preset=True
    )
    workload = app.extensions["inktime_render_workload_service"]
    monkeypatch.setattr(
        workload.process_boundary,
        "call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProcessCallTimeout("timeout")
        ),
    )
    with pytest.raises(ProcessCallTimeout):
        workload.test_release(settings, context)
    assert app.extensions["inktime_release_publisher"].list() == []
    assert DeviceTestReleaseStore(app.config["INKTIME_RELEASE_DIR"]).active(
        device_id, "gdep073e01_6c"
    ) is None
    assert str(
        app.extensions["inktime_settings_repository"].get(
            "render.custom_photo_presets", "{}"
        )
    ) == "{}"
    assert list(workload.prepared_root.iterdir()) == []


def test_test_release_revalidates_device_after_render(client, app, monkeypatch):
    create_admin(app)
    login(client)
    device_id, settings, context = _claimed_test_release(client, app)
    workload = app.extensions["inktime_render_workload_service"]
    original_get = workload.devices.get
    calls = 0

    def disable_after_render(selected_device_id):
        nonlocal calls
        calls += 1
        device = dict(original_get(selected_device_id))
        if calls > 1:
            device["enabled"] = 0
        return device

    monkeypatch.setattr(workload.devices, "get", disable_after_render)
    with pytest.raises(ValueError, match="DEVICE-006"):
        workload.test_release(settings, context)
    assert app.extensions["inktime_release_publisher"].list() == []
    assert DeviceTestReleaseStore(app.config["INKTIME_RELEASE_DIR"]).active(
        device_id, "gdep073e01_6c"
    ) is None
    assert list(workload.prepared_root.iterdir()) == []


def test_cancelled_late_render_result_is_deleted_without_publish(
    client, app, monkeypatch
):
    create_admin(app)
    login(client)
    device_id, settings, context = _claimed_test_release(client, app)
    workload = app.extensions["inktime_render_workload_service"]

    def finish_after_cancel(_function, *, kwargs, **_options):
        prepared = Path(kwargs["prepared_path"])
        prepared.mkdir(parents=True)
        (prepared / "late.tmp").write_bytes(b"late")
        app.extensions["inktime_job_repository"].cancel(context["job_id"])
        return {}

    monkeypatch.setattr(workload.process_boundary, "call", finish_after_cancel)
    with pytest.raises(ProcessCallError):
        workload.test_release(settings, context)
    assert app.extensions["inktime_release_publisher"].list() == []
    assert DeviceTestReleaseStore(app.config["INKTIME_RELEASE_DIR"]).active(
        device_id, "gdep073e01_6c"
    ) is None
    assert list(workload.prepared_root.iterdir()) == []


def test_expired_item_lease_rejects_test_release_commit(client, app, monkeypatch):
    create_admin(app)
    login(client)
    _device_id, settings, context = _claimed_test_release(client, app)
    workload = app.extensions["inktime_render_workload_service"]

    def finish_after_lease_loss(_function, *, kwargs, **_options):
        prepared = Path(kwargs["prepared_path"])
        prepared.mkdir(parents=True)
        with app.extensions["inktime_database"].session() as connection:
            connection.execute(
                "UPDATE job_items SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                (context["item_id"],),
            )
        return {}

    monkeypatch.setattr(workload.process_boundary, "call", finish_after_lease_loss)
    with pytest.raises(ProcessCallError):
        workload.test_release(settings, context)
    assert app.extensions["inktime_release_publisher"].list() == []
    assert list(workload.prepared_root.iterdir()) == []


def test_test_release_parent_commit_is_idempotent_for_same_item(client, app):
    create_admin(app)
    login(client)
    _device_id, settings, context = _claimed_test_release(
        client, app, save_preset=True
    )
    workload = app.extensions["inktime_render_workload_service"]
    first = workload.test_release(settings, context)
    second = workload.test_release(settings, context)
    assert second["release_id"] == first["release_id"]
    releases = app.extensions["inktime_release_publisher"].list()
    assert [item["release_id"] for item in releases] == [first["release_id"]]
    presets = json.loads(
        str(
            app.extensions["inktime_settings_repository"].get(
                "render.custom_photo_presets", "{}"
            )
        )
    )
    assert list(presets) == [first["saved_preset"]["id"]]
    assert list(workload.prepared_root.iterdir()) == []


def test_preset_write_failure_does_not_fail_assigned_test_release(
    client, app, monkeypatch
):
    create_admin(app)
    login(client)
    device_id, job_id = _queued_test_release(client, app, save_preset=True)
    workload = app.extensions["inktime_render_workload_service"]
    monkeypatch.setattr(
        workload.settings,
        "update",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private settings failure")
        ),
    )

    WorkerRunner(app).run_once()

    job = app.extensions["inktime_job_repository"].get(job_id)
    item = app.extensions["inktime_job_repository"].list_items(job_id)[0]
    result = json.loads(str(item["result_json"]))
    assert str(job["status"]) == "completed"
    assert result["preset_saved"] is False
    assert result["preset_error"] == "RENDER-PRESET-WRITE"
    assert len(app.extensions["inktime_release_publisher"].list()) == 1
    assert DeviceTestReleaseStore(app.config["INKTIME_RELEASE_DIR"]).active(
        device_id, "gdep073e01_6c"
    ) is not None
    with app.extensions["inktime_database"].session() as connection:
        warnings = connection.execute(
            "SELECT message,details_json FROM job_events "
            "WHERE job_id=? AND event='preset_warning'",
            (job_id,),
        ).fetchall()
    assert len(warnings) == 1
    assert "private settings failure" not in str(warnings[0]["details_json"])


def test_oversized_preset_does_not_block_test_release(client, app, monkeypatch):
    create_admin(app)
    login(client)
    device_id, job_id = _queued_test_release(client, app, save_preset=True)
    workload = app.extensions["inktime_render_workload_service"]
    original_get = workload.settings.get

    def oversized(key, default=None):
        if key == "render.custom_photo_presets":
            return json.dumps({"padding": {"value": "x" * 50_000}})
        return original_get(key, default)

    monkeypatch.setattr(workload.settings, "get", oversized)
    updates = []
    monkeypatch.setattr(
        workload.settings,
        "update",
        lambda *_args, **_kwargs: updates.append((_args, _kwargs)),
    )

    WorkerRunner(app).run_once()

    job = app.extensions["inktime_job_repository"].get(job_id)
    item = app.extensions["inktime_job_repository"].list_items(job_id)[0]
    result = json.loads(str(item["result_json"]))
    assert str(job["status"]) == "completed"
    assert result["preset_saved"] is False
    assert result["preset_error"] == "RENDER-PRESET-SIZE"
    assert updates == []
    assert len(app.extensions["inktime_release_publisher"].list()) == 1
    assert DeviceTestReleaseStore(app.config["INKTIME_RELEASE_DIR"]).active(
        device_id, "gdep073e01_6c"
    ) is not None
