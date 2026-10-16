from datetime import datetime, timedelta, timezone
from io import BytesIO
import os

import pytest


def _old(path):
    timestamp = (datetime.now(timezone.utc) - timedelta(days=3)).timestamp()
    os.utime(path, (timestamp, timestamp))


@pytest.mark.parametrize("status", ["pending", "preparing", "running", "pausing", "paused", "retrying", "budget_exceeded"])
def test_restarted_cleanup_preserves_resumable_job_inputs_under_age_and_capacity_pressure(app, status):
    workload = app.extensions["inktime_render_workload_service"]
    jobs = app.extensions["inktime_job_repository"]
    token, _ = workload.save_upload(BytesIO(b"synthetic-input"), suffix=".jpg", max_bytes=128)
    job_id = jobs.create_maintenance(kind="render_preview", name="retention", settings={"input_token": token}, created_by=None)
    workload.bind_input(token, job_id)
    path = workload.input_path(token, suffix=".jpg")
    _old(path)
    with jobs.database.transaction() as connection:
        connection.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    restarted = type(workload)(root=workload.root, publisher=workload.publisher, devices=workload.devices,
                               release_dir=workload.release_dir, settings_repository=workload.settings,
                               process_boundary=workload.process_boundary, job_repository=jobs)
    restarted.max_result_bytes = 1
    assert restarted.cleanup() == 0
    assert path.read_bytes() == b"synthetic-input"
    with pytest.raises(ValueError, match="RENDER-009"):
        restarted.save_upload(BytesIO(b"new"), suffix=".jpg", max_bytes=3)
    with jobs.database.transaction() as connection:
        connection.execute("UPDATE jobs SET status='completed' WHERE id=?", (job_id,))
    assert restarted.cleanup() == 1
    assert not path.exists()
    with jobs.database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM render_input_leases WHERE token=?", (token,)).fetchone()[0] == 0


def test_staging_reservation_protects_save_to_job_window_and_invalid_job_settings_fail_closed(app):
    workload = app.extensions["inktime_render_workload_service"]
    token, _ = workload.save_upload(BytesIO(b"staged"), suffix=".jpg", max_bytes=128)
    path = workload.input_path(token, suffix=".jpg")
    _old(path)
    assert workload.cleanup() == 0
    jobs = app.extensions["inktime_job_repository"]
    job_id = jobs.create_maintenance(kind="render_preview", name="unreadable", settings={"input_token": token}, created_by=None)
    workload.bind_input(token, job_id)
    with jobs.database.transaction() as connection:
        connection.execute("UPDATE jobs SET settings_json='not-json' WHERE id=?", (job_id,))
    assert workload.cleanup() == 0
    assert path.exists()


def test_failed_upload_releases_only_its_own_reservation_and_partial_file(app):
    workload = app.extensions["inktime_render_workload_service"]
    with pytest.raises(ValueError):
        workload.save_upload(BytesIO(b"too-large"), suffix=".jpg", max_bytes=1)
    assert list(workload.input_root.iterdir()) == []
    with workload.jobs.database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM render_input_leases").fetchone()[0] == 0
