"""Exercise the normal auto-publish path beyond an unpairable high-score burst."""
from __future__ import annotations

from PIL import Image

from tests.conftest import create_admin, csrf, login
from tests.integration.test_auto_photo_selection_regressions import _settings
from tests.unit.test_local_selection import _candidate
from inktime.app.domain.jobs.failure_policy import FailureClass, classify_codes
from inktime.app.domain.rendering.adaptive_layout import InsufficientPhotoContentError
from inktime.app.workers.runner import WorkerRunner


def test_auto_publish_searches_past_duplicate_burst_for_landscape_frames(app, tmp_path, monkeypatch):
    actor = _settings(app, {"render.history_today_fallback": "ranked"})
    root = tmp_path / "photos"
    root.mkdir()
    month_day = app.extensions["inktime_render_service"]._today().strftime("%m-%d")

    for index in range(4):
        photo_id = f"burst-{index}"
        _candidate(app, root, photo_id, 100, month_day)
    for index in range(2):
        photo_id = f"wide-{index}"
        _candidate(app, root, photo_id, 30, month_day)
        Image.new("RGB", (1600, 900), "#4477aa").save(root / f"{photo_id}.jpg")

    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET duplicate_group_id='same-burst' WHERE id LIKE 'burst-%'"
        )
        connection.execute(
            "UPDATE photos SET width=1600,height=900 WHERE id LIKE 'wide-%'"
        )

    service = app.extensions["inktime_render_service"]
    observed = []

    def capture(plan, **_kwargs):
        observed.append(plan)
        return Image.new("RGB", (480, 800), "white")

    monkeypatch.setattr(service, "_render_plan_image", capture)
    service.publish([], actor, quantity_override=2, profile_keys=["safe_4c"])

    assert len(observed) == 2
    assert [plan["primary_photo_id"] for plan in observed] == ["wide-0", "wide-1"]
    assert all(plan["layout"] == "full" and plan["secondary_photo_id"] is None for plan in observed)
    assert all(plan["orientation"] == "landscape" for plan in observed)
    for plan in observed:
        with Image.open(root / f"{plan['primary_photo_id']}.jpg") as photo:
            assert photo.width > photo.height


def test_auto_photo_shortage_is_terminal_for_render_worker(app, client, monkeypatch):
    create_admin(app)
    login(client)

    def fail_publish(*_args, **_kwargs):
        raise InsufficientPhotoContentError("RENDER-005 insufficient candidates")

    monkeypatch.setattr(app.extensions["inktime_render_service"], "publish", fail_publish)
    response = client.post(
        "/api/v1/releases",
        json={"photo_ids": []},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 202
    job_id = response.get_json()["id"]

    assert WorkerRunner(app, isolate_local=False).run_once() == 1
    item = app.extensions["inktime_job_repository"].list_items(job_id)[0]
    assert item["error_code"] == "NO_ELIGIBLE_CANDIDATES"
    assert item["status"] == "failed"
    assert classify_codes([item["error_code"]]) is FailureClass.TERMINAL_NO_RETRY
