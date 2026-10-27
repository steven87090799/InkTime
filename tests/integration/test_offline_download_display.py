from datetime import datetime
from zoneinfo import ZoneInfo

from tests.conftest import create_admin, login
from tests.integration.test_offline_queue_ack import _ack, _release


def test_offline_verified_download_is_not_displayed_as_never_downloaded(custom_device_schedule, app):
    create_admin(app)
    device_id, token = app.extensions["inktime_device_repository"].create(
        "Offline status audit", delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True, schedule_times=["23:59"],
    )
    release = _release(app, "offline-status-audit")
    app.extensions["inktime_offline_schedule_repository"].prepare_day(
        device_id=device_id, target_date=datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat(),
        release_ids=[release["release_id"]],
    )
    with app.extensions["inktime_database"].session() as connection:
        item = dict(connection.execute("SELECT * FROM device_content_queue_items WHERE device_id=?", (device_id,)).fetchone())
        version = connection.execute("SELECT queue_version FROM device_content_queues WHERE device_id=?", (device_id,)).fetchone()[0]
    client = app.test_client()
    for index, event in enumerate(("MANIFEST_RECEIVED", "DOWNLOAD_COMPLETED", "HASH_VERIFIED")):
        assert _ack(client, token, item["id"], version, event, f"status-{index}").status_code == 200
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT status FROM device_content_queue_items WHERE id=?", (item["id"],)).fetchone()[0] == "ACKNOWLEDGED"
    login(client)
    response = client.get("/devices")
    assert response.status_code == 200
    assert "1／1 已確認下載" in response.get_data(as_text=True)
    assert release["release_id"] in response.get_data(as_text=True)
    assert "下載確認不代表已顯示" in response.get_data(as_text=True)
    assert "尚未下載" not in response.get_data(as_text=True), "A hash-verified offline download must not be labelled never downloaded"
