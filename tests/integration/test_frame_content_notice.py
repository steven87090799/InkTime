from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


def _device(app):
    device_id, token = app.extensions["inktime_device_repository"].create(
        "底部提示", timezone_name="Asia/Taipei", delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True, schedule_times=["08:00"],
    )
    version = int(app.extensions["inktime_device_repository"].get(device_id)["config_version"])
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    return device_id, token, version, today


def _shortage(app, device_id, version, target):
    app.extensions["inktime_offline_schedule_repository"].record_terminal_outcome(
        device_id=device_id, target_date=target.isoformat(), config_version=version,
        outcome_code="NO_ELIGIBLE_CANDIDATES", message="test shortage",
    )


def test_unprepared_or_future_playlist_does_not_show_no_photos(client, app):
    device_id, token, version, today = _device(app)
    headers = {"Authorization": f"Bearer {token}"}
    path = "/api/device/v1/offline-schedule"
    response = client.get(path, headers=headers)
    assert response.status_code == 404
    assert "content_notice" not in response.get_json()
    _shortage(app, device_id, version, today + timedelta(days=1))
    response = client.get(path + "?target=next", headers=headers)
    assert response.status_code == 404
    assert "content_notice" not in response.get_json()
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("state,expected", [
    ("unscanned", "no_eligible_photos"),
    ("verified_empty", "empty_library"),
    ("failed_scan", "no_eligible_photos"),
    ("unmounted", "no_eligible_photos"),
    ("analysis_missing", "analysis_required"),
])
def test_confirmed_shortage_distinguishes_empty_from_unanalysed_library(client, app, tmp_path, state, expected):
    device_id, token, version, today = _device(app)
    root = tmp_path / "notice-photos"
    root.mkdir()
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("照片庫", root)
    now = datetime.now(ZoneInfo("UTC")).isoformat()
    if state != "unscanned":
        scan_id = photos.begin_scan(library_id, root, mode="full", trigger_source="test", missing_threshold_ratio=0.1)
        with app.extensions["inktime_database"].session() as connection:
            connection.execute(
                "UPDATE scan_runs SET status=?,full_census=1,completed_at=? WHERE id=?",
                ("failed" if state == "failed_scan" else "completed", now, scan_id),
            )
    if state == "analysis_missing":
        with app.extensions["inktime_database"].session() as connection:
            connection.execute(
                "INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at) "
                "VALUES ('present-photo',?,'present.jpg','preprocessed',?,?)", (library_id, now, now),
            )
    if state == "unmounted":
        root.rmdir()
    _shortage(app, device_id, version, today)
    response = client.get("/api/device/v1/offline-schedule", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404
    body = response.get_json()
    assert len(response.data) <= 4096
    assert body["no_content"] is True
    assert body["content_notice"]["kind"] == expected
    assert body["content_notice"]["target_date"] == today.isoformat()
    assert body["content_notice"]["config_version"] == version
    assert "播放完" not in body["content_notice"]["message"]
    assert int(response.headers["Retry-After"]) >= 1
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM display_history").fetchone()[0] == 0


def test_shortage_notice_is_invalidated_by_new_config_or_success(app):
    device_id, _, version, today = _device(app)
    schedules = app.extensions["inktime_offline_schedule_repository"]
    _shortage(app, device_id, version, today)
    args = {"device_id": device_id, "target_date": today.isoformat(), "config_version": version}
    assert schedules.content_notice_for_device(**args) is not None
    assert schedules.content_notice_for_device(**{**args, "config_version": version + 1}) is None
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE device_offline_schedules SET status='ready',terminal_outcome_code=NULL WHERE device_id=?", (device_id,))
    assert schedules.content_notice_for_device(**args) is None
