from datetime import datetime, timedelta
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from PIL import Image
import pytest

from tests.unit.test_analysis_schema import valid_result


def _set(app, key, value):
    app.extensions["inktime_settings_repository"].update(key, value, changed_by="test", source_ip="test")


def _photo(app, root, photo_id="waiting-photo"):
    root.mkdir(exist_ok=True)
    path = root / f"{photo_id}.jpg"
    Image.new("RGB", (640, 480), "#527f99").save(path)
    library = app.extensions["inktime_photo_repository"].ensure_library("照片版", root)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """INSERT INTO photos(id,library_id,relative_path,status,sha256,width,height,
                eligible,local_features_status,local_candidate_score,screenshot_likelihood,
                captured_at,captured_date,captured_month_day,created_at,updated_at)
                VALUES (?,?,?,'preprocessed',?,640,480,1,'complete',80,0,
                        '2020-07-22T10:00:00','2020-07-22','07-22','2026-01-01','2026-01-01')""",
            (photo_id, library, path.name, sha256(path.read_bytes()).hexdigest()),
        )
    return photo_id


def _analysis(app, photo_id, **changes):
    result = valid_result(side_caption="這是後臺模型產生的一句話。", **changes)
    app.extensions["inktime_photo_repository"].save_analysis(
        photo_id, None, "single", "test-provider", "test-model", result,
        json.dumps(result, ensure_ascii=False),
    )


@pytest.mark.parametrize("background_mode", ["disabled", "local_only", "local_with_manual_ai", "automatic_ai", "seasonal_ai"])
def test_photo_only_selects_before_model_regardless_of_background_mode(app, tmp_path, background_mode):
    photo_id = _photo(app, tmp_path / "local")
    _set(app, "render.content_mode", "photo_only")
    _set(app, "analysis.execution_mode", background_mode)
    service = app.extensions["inktime_render_service"]
    assert service.select_candidates(1) == [photo_id]
    assert app.extensions["inktime_settings_repository"].get("analysis.execution_mode") == background_mode
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photo_analysis").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_photo_only_can_publish_and_download_while_still_pending_background_analysis(client, app, tmp_path):
    photo_id = _photo(app, tmp_path / "publish")
    _set(app, "render.content_mode", "photo_only")
    _set(app, "render.layout", "auto_photo")
    device_id, token = app.extensions["inktime_device_repository"].create(
        "照片版相框", panel_profile="safe_4c", layout_mode="auto_photo",
        frame_orientation="landscape",
    )
    release = app.extensions["inktime_render_service"].publish(
        [photo_id], "test", device_ids=[device_id], quantity_override=1,
    )
    manifest = app.extensions["inktime_release_publisher"].validate(release["release_id"])
    options = manifest["render_options"]
    assert options["content_mode"] == "photo_only"
    plan = options["render_plans"][0]
    assert plan["primary_caption"]["text"] == ""
    assert plan["primary_eligibility_source"] == "local"
    filename = next(entry["name"] for entry in manifest["files"] if entry["name"].endswith(".bin"))
    url = f"/api/device/v1/releases/{release['release_id']}/files/{filename}"
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(url, headers=headers).status_code == 200
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET display_count=1,last_displayed_at='2026-01-02' WHERE id=?", (photo_id,))
    pending = app.extensions["inktime_job_repository"].iter_pending_photo_ids(analysis_fingerprint="future-v6")
    assert photo_id in set(pending)  # displaying a photo never completes/skips its model work
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT status FROM photos WHERE id=?", (photo_id,)).fetchone()[0] == "preprocessed"
    _analysis(app, photo_id, display_exclusion={"category": "screenshot", "confidence": 0.99})
    assert client.get(url, headers=headers).status_code == 404  # later model exclusions still revoke delivery


def test_photo_only_hides_model_manual_and_fallback_captions_without_disabling_analysis(app, tmp_path):
    photo_id = _photo(app, tmp_path / "captions")
    _set(app, "analysis.execution_mode", "automatic_ai")
    _set(app, "render.content_mode", "photo_only")
    service = app.extensions["inktime_render_service"]
    empty = service.preview_fingerprint(photo_id, layout="auto_photo")["render_plan"]
    assert empty["primary_caption"]["text"] == ""  # no generated local fallback
    _analysis(app, photo_id)
    complete = service.preview_fingerprint(photo_id, layout="auto_photo")["render_plan"]
    assert complete["primary_caption"]["text"] == ""
    assert complete["photo_footer_parts"][photo_id]["caption"] == ""
    injected = {**complete["primary_caption"], "text": "既有工作保存的舊短句"}
    assert service._bound_caption_record(injected, photo_id)["text"] == ""
    _set(app, "render.content_mode", "photo_caption")
    caption = service.preview_fingerprint(photo_id, layout="auto_photo")["render_plan"]
    assert caption["primary_caption"]["text"]
    assert app.extensions["inktime_settings_repository"].get("analysis.execution_mode") == "automatic_ai"


@pytest.mark.parametrize("hold", ["excluded", "missing", "screenshot", "unfinished"])
def test_photo_only_keeps_local_safety_holds(app, tmp_path, hold):
    photo_id = _photo(app, tmp_path / "holds")
    queries = {
        "excluded": "UPDATE photos SET eligible=0,exclusion_status='manually_excluded' WHERE id=?",
        "missing": "UPDATE photos SET lifecycle_status='missing' WHERE id=?",
        "screenshot": "UPDATE photos SET screenshot_likelihood=1 WHERE id=?",
        "unfinished": "UPDATE photos SET local_features_status='pending' WHERE id=?",
    }
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(queries[hold], (photo_id,))
    _set(app, "render.content_mode", "photo_only")
    assert app.extensions["inktime_render_service"].select_candidates(1) == []
    with pytest.raises(ValueError, match="RENDER-009"):
        app.extensions["inktime_render_candidate_repository"].require_for_execution_mode(
            [photo_id], "automatic_ai", content_mode="photo_only",
        )


def test_offline_photo_only_playlist_is_ready_without_model_results(app, tmp_path):
    _photo(app, tmp_path / "offline")
    _set(app, "render.content_mode", "photo_only")
    device_id, _ = app.extensions["inktime_device_repository"].create(
        "離線照片版", panel_profile="safe_4c", layout_mode="auto_photo", frame_orientation="landscape",
        delivery_mode="inktime_offline_schedule", offline_prefetch_allowed=True, schedule_times=["08:00"],
    )
    tomorrow = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    result = app.extensions["inktime_display_preparation_service"].prepare_device_day(
        device_id=device_id, target_date=tomorrow.isoformat(), created_by="test",
    )
    assert result["status"] == "ready"
    assert result["output_count"] == 1
