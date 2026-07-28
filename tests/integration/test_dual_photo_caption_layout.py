from __future__ import annotations

from pathlib import Path

from PIL import Image

from inktime.app.workers.runner import WorkerRunner
from tests.conftest import create_admin, csrf, login


def _photo(app, root: Path, photo_id: str, size: tuple[int, int], captured_at: str, color: str):
    Image.new("RGB", size, color).save(root / f"{photo_id}.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("雙文字測試", root)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """INSERT INTO photos(id,library_id,relative_path,width,height,status,eligible,lifecycle_status,
                exclusion_status,local_features_status,local_candidate_score,captured_at,captured_date,captured_month_day,created_at,updated_at)
               VALUES (?,?,?,?,?,'analyzed',1,'active','eligible','complete',88,?,?,?,?,?)""",
            (photo_id, library_id, f"{photo_id}.jpg", *size, captured_at, captured_at[:10], captured_at[5:10], captured_at, captured_at),
        )


def test_pair_caption_plan_keeps_two_independent_records_and_fingerprint(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _photo(app, root, "a", (1600, 900), "2021-07-28T10:00:00+00:00", "#4477aa")
    _photo(app, root, "b", (900, 1600), "2020-01-02T10:00:00+00:00", "#aa7744")
    service = app.extensions["inktime_render_service"]
    portrait = service.resolve_render_plan("a", layout="photo_pair_caption", secondary_photo_id="b", orientation="portrait")
    landscape = service.resolve_render_plan("a", layout="photo_pair_caption", secondary_photo_id="b", orientation="landscape")
    assert portrait["primary_caption"]["photo_id"] == "a"
    assert portrait["secondary_caption"]["photo_id"] == "b"
    assert portrait["primary_caption"] is not portrait["secondary_caption"]
    assert portrait["primary_caption_text_hash"] != portrait["secondary_caption_text_hash"]
    assert service.render_photo("a", layout="photo_pair_caption", secondary_photo_id="b", orientation="portrait").size == (480, 800)
    assert service.render_photo("a", layout="photo_pair_caption", secondary_photo_id="b", orientation="landscape").size == (480, 800)
    assert portrait["orientation"] != landscape["orientation"]
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.layout", "photo_pair_caption", changed_by="test", source_ip="127.0.0.1")
    release = service.publish(["a", "b"], "test")
    manifest = app.extensions["inktime_release_publisher"].validate(release["release_id"])
    plan = manifest["render_options"]["render_plans"][0]
    assert plan["primary_caption"]["photo_id"] == "a"
    assert plan["secondary_caption"]["photo_id"] == "b"


def test_dual_pair_compare_uses_one_frozen_pair_for_four_formal_previews(client, app, tmp_path):
    root = tmp_path / "compare"
    root.mkdir()
    _photo(app, root, "a", (1600, 900), "2021-07-28T10:00:00+00:00", "#4477aa")
    _photo(app, root, "b", (900, 1600), "2020-01-02T10:00:00+00:00", "#aa7744")
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/rendering/dual-pair-compare",
        json={"primary_photo_id": "a", "secondary_photo_id": "b", "profile": "gdep073e01_6c", "fit_mode": "contain"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 202
    job_id = response.json["id"]
    assert WorkerRunner(app).run_once() == 1
    status = client.get(f"/api/v1/jobs/{job_id}").json
    previews = status["result"]["previews"]
    assert {(item["layout"], item["orientation"]) for item in previews} == {
        ("photo_pair", "portrait"), ("photo_pair", "landscape"),
        ("photo_pair_caption", "portrait"), ("photo_pair_caption", "landscape"),
    }
    assert {item["primary_photo_id"] for item in previews} == {"a"}
    assert {item["secondary_photo_id"] for item in previews} == {"b"}
    assert len({item["effective_dither"] for item in previews}) == 1
    captions = [item for item in previews if item["layout"] == "photo_pair_caption"]
    assert len({item["primary_caption"]["text_hash"] for item in captions}) == 1
    assert len({item["secondary_caption"]["text_hash"] for item in captions}) == 1
    assert all(client.get(item["preview_url"]).status_code == 200 for item in previews)
