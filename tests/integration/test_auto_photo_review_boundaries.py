from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from inktime.app.domain.rendering.adaptive_layout import InsufficientPhotoContentError
import inktime.app.services.budgets as budgets_module
from inktime.app.services.usage_periods import usage_periods
from tests.integration.test_adaptive_frame_renderer import _analyzed_photo
from tests.integration.test_auto_photo_selection_regressions import _settings
from tests.integration.test_offline_queue_ack import _ack, _release
from tests.conftest import create_admin


@pytest.mark.parametrize("mode", ["local_only", "automatic_ai"])
@pytest.mark.parametrize("fallback", ["none", "nearby_only", "ranked"])
def test_preview_automatic_partner_obeys_date_fallback(app, tmp_path, monkeypatch, mode, fallback):
    _settings(app, {"analysis.execution_mode": mode, "render.history_today_fallback": fallback})
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2024-10-17T10:00:00+00:00")
    _analyzed_photo(app, root, "outside", (900, 1600), "2024-01-01T10:00:00+00:00")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET local_candidate_score=80,captured_date=substr(captured_at,1,10), "
                           "captured_month_day=substr(captured_at,6,5),capture_date_status='valid'")
    service = app.extensions["inktime_render_service"]
    monkeypatch.setattr(service, "_today", lambda: date(2026, 10, 17))
    if fallback == "ranked":
        plan = service.resolve_render_plan("primary", layout="auto_photo", orientation="landscape")
        assert plan["secondary_photo_id"] == "outside"
    else:
        with pytest.raises(InsufficientPhotoContentError):
            service.resolve_render_plan("primary", layout="auto_photo", orientation="landscape")


@pytest.mark.parametrize("timestamp,daily,monthly", [
    ("2026-10-17T00:00:00+08:00", 0.2, 0.2),
    ("2026-10-16T16:00:00Z", 0.2, 0.2),
    ("2026-10-16T12:00:00-04:00", 0.2, 0.2),
    ("2026-10-16T23:59:59.999+08:00", 0, 0.2),
    ("2026-10-18T00:00:00+08:00", 0, 0.2),
    ("2026-10-01T00:00:00+08:00", 0, 0.2),
    ("2026-09-30T23:59:59.999+08:00", 0, 0),
    ("2026-11-01T00:00:00+08:00", 0, 0),
])
def test_budget_offsets_respect_half_open_day_and_month(app, monkeypatch, timestamp, daily, monthly):
    periods = usage_periods("Asia/Taipei", now=datetime.fromisoformat("2026-10-17T12:30:00+00:00"))
    monkeypatch.setattr(budgets_module, "usage_periods", lambda _zone: periods)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "INSERT INTO api_usage(provider,model,request_type,started_at,status,cost_source,estimated_cost) "
            "VALUES ('test','test','vision',?,'completed','estimated',0.2)", (timestamp,),
        )
    snapshot = app.extensions["inktime_budget_service"].snapshot()
    assert snapshot["daily_effective"] == pytest.approx(daily)
    assert snapshot["monthly_effective"] == pytest.approx(monthly)


def test_offline_download_summary_requires_hash_and_current_date_and_config(custom_device_schedule, app):
    create_admin(app)
    device_id, token = app.extensions["inktime_device_repository"].create(
        "Offline evidence", delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True, schedule_times=["23:59"],
    )
    release = _release(app, "download-evidence")
    schedules = app.extensions["inktime_offline_schedule_repository"]
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    target = today.isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    schedules.prepare_day(device_id=device_id, target_date=target, release_ids=[release["release_id"]])
    with app.extensions["inktime_database"].session() as connection:
        item = dict(connection.execute("SELECT * FROM device_content_queue_items WHERE device_id=?", (device_id,)).fetchone())
        version = connection.execute("SELECT queue_version FROM device_content_queues WHERE device_id=?", (device_id,)).fetchone()[0]
        config = connection.execute("SELECT config_version FROM devices WHERE id=?", (device_id,)).fetchone()[0]
    client = app.test_client()
    for index, event in enumerate(("MANIFEST_RECEIVED", "DOWNLOAD_COMPLETED")):
        assert _ack(client, token, item["id"], version, event, f"evidence-{index}").status_code == 200
    assert schedules.download_summary(device_id, target_date=target, config_version=config)["verified"] == 0
    assert _ack(client, token, item["id"], version, "HASH_VERIFIED", "evidence-hash").status_code == 200
    assert schedules.download_summary(device_id, target_date=target, config_version=config)["verified"] == 1
    assert schedules.download_summary(device_id, target_date=tomorrow, config_version=config)["verified"] == 0
    assert schedules.download_summary(device_id, target_date=target, config_version=config + 1)["verified"] == 0
