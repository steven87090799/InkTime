from pathlib import Path

import pytest

from tests.conftest import create_admin, csrf, login


@pytest.mark.parametrize("path", ["/photos", "/maintenance"])
def test_scan_controls_show_actual_schedule_without_starting_work(client, app, path):
    create_admin(app)
    login(client)
    schedules = app.extensions["inktime_schedule_repository"]
    before = schedules.get("incremental_scan")
    body = client.get(path).get_data(as_text=True)
    assert 'id="scan-form"' in body
    assert 'id="photo-scan-schedule"' in body
    assert "立即掃描照片" in body
    assert "每月 1 日 03:00" in body
    assert "尚未建立掃描工作" in body
    assert "photo-scan.js" in body
    assert schedules.get("incremental_scan") == before
    assert app.extensions["inktime_job_repository"].list() == []


def test_scan_panel_preserves_custom_cron_and_schedule_options(client, app):
    create_admin(app)
    login(client)
    schedules = app.extensions["inktime_schedule_repository"]
    before = schedules.update("incremental_scan", {"cron": "*/30 * * * *", "weekdays": [2]}, "Asia/Taipei")
    body = client.get("/photos").get_data(as_text=True)
    assert "自訂排程（*/30 * * * *）" in body
    assert 'value="custom" selected' in body
    assert schedules.get("incremental_scan") == before


def test_failed_source_job_is_visible_even_before_scan_record_exists(client, app):
    admin = create_admin(app)
    login(client)
    jobs = app.extensions["inktime_job_repository"]
    root = str(Path(app.config["INKTIME_PHOTO_DIR"]).resolve())
    job_id = jobs.create_maintenance(kind="virtual_display", name="test", settings={"root_path": root}, created_by=admin)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE jobs SET status='completed_with_errors',failed_items=1 WHERE id=?", (job_id,))
        connection.execute(
            """INSERT INTO job_errors(job_id,component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at)
               VALUES (?,'worker','SCAN-002','source-denied','error','ScanSourceError',?,?)""",
            (job_id, "2026-10-25T08:59:00+00:00", "2026-10-25T08:59:00+00:00"),
        )
    body = client.get("/photos").get_data(as_text=True)
    assert "工作完成，但有失敗項目" in body
    assert f'href="/jobs/{job_id}"' in body
    assert "尚無可核對的掃描紀錄" in body
    assert "包含掃描及虛擬墨水屏發布" in body
    assert "照片來源無法完整掃描" in body
    assert "資料夾權限" in body


def test_scan_summary_uses_configured_source_and_does_not_mix_other_library_jobs(client, app, tmp_path):
    admin = create_admin(app)
    login(client)
    jobs = app.extensions["inktime_job_repository"]
    jobs.create_maintenance(kind="scan", name="other", settings={"root_path": str(tmp_path / "other")}, created_by=admin)
    body = client.get("/photos").get_data(as_text=True)
    assert "尚未建立掃描工作" in body


def test_daily_preset_can_use_existing_schedule_api_without_losing_scan_settings(client, app):
    create_admin(app)
    login(client)
    schedules = app.extensions["inktime_schedule_repository"]
    before = schedules.get("incremental_scan")
    response = client.patch("/api/v1/schedules/incremental_scan", json={
        "enabled": True, "cron": "0 3 * * *", "start_time": "03:00",
        "weekdays": [], "window_start": None, "window_end": None,
    }, headers={"X-CSRF-Token": csrf(client)})
    assert response.status_code == 200
    saved = response.get_json()["task"]
    assert saved["cron"] == "0 3 * * *"
    assert saved["config"] == before["config"]
    assert saved["retry_count"] == before["retry_count"]
    assert saved["next_run"]
    assert "每天 03:00" in client.get("/maintenance").get_data(as_text=True)


def test_viewer_sees_scan_guidance_without_mutating_controls(client, app):
    app.extensions["inktime_auth_repository"].create_user("viewer", "very-safe-passphrase", role="viewer")
    login(client, username="viewer")
    body = client.get("/photos").get_data(as_text=True)
    assert "照片來源與掃描" in body
    assert 'id="scan-form"' not in body
    assert 'id="photo-scan-schedule"' not in body


@pytest.mark.parametrize("path", ["/photos", "/maintenance"])
def test_invalid_historical_scan_settings_cannot_break_photo_navigation(client, app, path):
    admin = create_admin(app)
    login(client)
    jobs = app.extensions["inktime_job_repository"]
    job_id = jobs.create_maintenance(kind="scan", name="corrupt history", settings={}, created_by=admin)
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute("UPDATE jobs SET settings_json='{' WHERE id=?", (job_id,))
    response = client.get(path)
    assert response.status_code == 200
    assert 'href="/analysis/missing"' in response.get_data(as_text=True)


def test_scan_frequency_preserves_backfill_profile_and_seasonal_mode(client, app):
    admin = create_admin(app)
    login(client)
    service = app.extensions["inktime_analysis_backfill_service"]
    service.configure({"model": "mock/free", "api_key": "synthetic-private-key", "concurrency": 2, "rpm": 20}, admin)
    service.set_enabled(True, admin)
    settings = app.extensions["inktime_settings_repository"]
    schedules = app.extensions["inktime_schedule_repository"]
    mode = settings.get("analysis.execution_mode")
    window = settings.get("analysis.seasonal_window_days")
    reconcile = schedules.get("full_reconcile")
    before = service.profile()
    response = client.patch("/api/v1/schedules/incremental_scan", json={
        "enabled": True, "cron": "0 3 * * *", "start_time": "03:00",
        "weekdays": [], "window_start": None, "window_end": None,
    }, headers={"X-CSRF-Token": csrf(client)})
    assert response.status_code == 200
    assert settings.get("analysis.execution_mode") == mode == "seasonal_ai"
    assert settings.get("analysis.seasonal_window_days") == window == 5
    assert schedules.get("full_reconcile") == reconcile
    assert service.profile() == before
    page = client.get("/photos").get_data(as_text=True)
    assert 'href="/analysis/missing"' in page and 'id="photo-scan"' in page
    assert "synthetic-private-key" not in page
