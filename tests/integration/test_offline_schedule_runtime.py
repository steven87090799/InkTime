from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image
import pytest


def _release(app, name: str) -> dict:
    staged = app.extensions["inktime_release_publisher"].publish(
        [(name, Image.new("RGB", (480, 800), "white"))],
        profile_key="safe_4c",
        activate=False,
    )
    return app.extensions["inktime_release_coordinator"].publish(
        [staged], created_by="offline-test", photo_ids=[]
    )[0]


def test_offline_day_preparation_is_atomic_and_device_projection_contains_full_sha(client, app):
    devices = app.extensions["inktime_device_repository"]
    device_id, token = devices.create(
        "離線相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
        prefetch_lead_minutes=5,
        button_wake_action="local_next",
    )
    first = _release(app, "offline-first")
    second = _release(app, "offline-second")
    repository = app.extensions["inktime_offline_schedule_repository"]

    prepared = repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-03",
        release_ids=[first["release_id"], second["release_id"]],
    )
    same_day = repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-03",
        release_ids=[first["release_id"], second["release_id"]],
    )

    assert prepared["schedule"]["status"] == "ready"
    assert same_day["schedule"]["id"] == prepared["schedule"]["id"]
    assert len(prepared["slots"]) == 2
    assert all(len(slot["sha256"]) == 64 for slot in prepared["slots"])
    assert all(slot["show_at"].endswith("+00:00") for slot in prepared["slots"])
    with app.extensions["inktime_database"].session() as connection:
        queue_items = connection.execute(
            """
            SELECT delivery_mode,offline_prefetch_allowed,offline_slot,offline_schedule_id,
                   terminal_ack_retention
            FROM device_content_queue_items WHERE device_id=? ORDER BY position
            """,
            (device_id,),
        ).fetchall()
    assert [(row["delivery_mode"], row["offline_prefetch_allowed"]) for row in queue_items] == [
        ("offline_schedule", 1),
        ("offline_schedule", 1),
    ]
    assert all(row["offline_schedule_id"] == prepared["schedule"]["id"] for row in queue_items)
    assert all(row["terminal_ack_retention"] for row in queue_items)
    assert prepared["device"]["rotation"] == 0

    response = client.get(
        "/api/device/v1/offline-schedule",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["schema_version"] == 1
    assert body["schedule_id"] == prepared["schedule"]["id"]
    assert body["target_local_date"] == "2026-08-03"
    assert body["panel_profile"] == "safe_4c"
    assert body["rotation"] == 0
    assert body["target_start_epoch"] < body["slots"][0]["show_at_epoch"] < body["target_end_epoch"]
    assert body["target"] == "current"
    slot_zone = datetime.fromisoformat(body["slots"][0]["show_at"]).tzinfo
    assert body["next_target_start_epoch"] == int(
        datetime(2026, 8, 4, 0, 0, tzinfo=slot_zone).timestamp()
    )
    assert body["next_target_start_epoch"] < body["next_schedule_prefetch_epoch"] < int(
        datetime(2026, 8, 4, 8, 0, tzinfo=slot_zone).timestamp()
    )
    assert body["slots"][0]["show_at_epoch"] == int(
        datetime.fromisoformat(body["slots"][0]["show_at"]).timestamp()
    )
    assert body["schedule_times"] == ["08:00", "20:00"]
    assert body["prefetch_lead_minutes"] == 5
    assert body["slots"][0]["sha256"] == prepared["slots"][0]["sha256"]
    assert body["slots"][0]["show_at"].endswith("+08:00")
    assert body["slots"][0]["download_url"].startswith("/api/device/v1/queue/items/")
    assert body["slots"][0]["size"] == 96_000
    generic_manifest = client.get(
        "/api/device/v1/queue/manifest",
        headers={"Authorization": f"Bearer {token}"},
    ).get_json()
    assert generic_manifest["items"] == []

    third = _release(app, "offline-third")
    fourth = _release(app, "offline-fourth")
    next_day = repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-04",
        release_ids=[third["release_id"], fourth["release_id"]],
    )
    assert next_day["schedule"]["target_date"] == "2026-08-04"
    with app.extensions["inktime_database"].session() as connection:
        statuses = connection.execute(
            "SELECT status FROM device_content_queue_items WHERE device_id=? ORDER BY position",
            (device_id,),
        ).fetchall()
    assert [row["status"] for row in statuses] == ["READY", "READY", "READY", "READY"]
    still_today = client.get(
        "/api/device/v1/offline-schedule",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert still_today.status_code == 200
    assert still_today.get_json()["schedule_id"] == prepared["schedule"]["id"]
    assert still_today.get_json()["target_local_date"] == "2026-08-03"
    next_response = client.get(
        "/api/device/v1/offline-schedule?target=next",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert next_response.status_code == 200
    next_body = next_response.get_json()
    assert next_body["target"] == "next"
    assert next_body["target_local_date"] == "2026-08-04"
    assert next_body["schedule_id"] == next_day["schedule"]["id"]
    for invalid_target in ("2026-08-05", "+1", "history"):
        assert client.get(
            f"/api/device/v1/offline-schedule?target={invalid_target}",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code == 400


def test_offline_schedule_snapshot_does_not_follow_live_device_config(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create(
        "離線快照",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
        rotation=0,
    )
    release = _release(app, "offline-snapshot")
    repository = app.extensions["inktime_offline_schedule_repository"]
    prepared = repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-03",
        release_ids=[release["release_id"]],
    )
    app.extensions["inktime_device_repository"].update(
        device_id,
        name="離線快照已變更",
        enabled=True,
        timezone_name="Asia/Taipei",
        schedule="08:00",
        schedule_times=["08:00"],
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        rotation=180,
        panel_profile="safe_4c",
        prefetch_lead_minutes=5,
        button_wake_action="check_new",
    )
    stored = repository.ready_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=int(prepared["schedule"]["config_version"]),
    )
    assert stored is not None
    assert stored["device"]["rotation"] == 0
    assert repository.latest_for_device(device_id)["schedule"]["id"] == prepared["schedule"]["id"]
    assert client.get(
        "/api/device/v1/offline-schedule",
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 404


def test_missing_today_schedule_returns_bounded_server_retry_epoch(client, app):
    _device_id, token = app.extensions["inktime_device_repository"].create(
        "離線尚未準備",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
        prefetch_lead_minutes=5,
    )
    before = int(datetime.now().timestamp())
    response = client.get(
        "/api/device/v1/offline-schedule",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = response.get_json()
    assert response.status_code == 404
    assert body["error"] == "schedule_not_ready"
    assert body["error_code"] == "DEVICE-008"
    assert body["target"] == "current"
    assert body["target_date"] == "2026-08-03"
    assert body["retry_after_epoch"] > before
    assert "next_slot_epoch" in body
    assert int(response.headers["Retry-After"]) >= 1
    next_response = client.get(
        "/api/device/v1/offline-schedule?target=next",
        headers={"Authorization": f"Bearer {token}"},
    )
    next_body = next_response.get_json()
    assert next_response.status_code == 404
    assert next_body["target"] == "next"
    assert next_body["target_date"] == "2026-08-04"
    assert next_body["retry_after_epoch"] < int(
        datetime(2026, 8, 4, 8, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )


def test_offline_schedule_repository_fails_closed_on_corrupt_manifest_and_keeps_snapshot_typed(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線完整性邊界",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    release = _release(app, "offline-corruptible")
    repository = app.extensions["inktime_offline_schedule_repository"]
    prepared = repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-03",
        release_ids=[release["release_id"]],
    )

    with app.extensions["inktime_database"].session() as connection:
        assert repository._row(connection, "missing-schedule") is None
    with pytest.raises(ValueError, match="不可解析"):
        repository._manifest_entry("not-json")

    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE device_offline_schedules SET snapshot_json='not-json' WHERE id=?",
            (prepared["schedule"]["id"],),
        )
    snapshot = repository.ready_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=int(prepared["schedule"]["config_version"]),
    )
    assert snapshot is not None
    assert snapshot["device"]["snapshot_json"] == {}

    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE releases SET manifest_json='not-json' WHERE id=?", (release["release_id"],)
        )
    with pytest.raises(ValueError, match="Manifest"):
        repository.ready_for_device(
            device_id=device_id,
            target_date="2026-08-03",
            config_version=int(prepared["schedule"]["config_version"]),
        )

    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE releases SET manifest_json=? WHERE id=?",
            ('{"files": []}', release["release_id"]),
        )
    with pytest.raises(ValueError, match="Manifest"):
        repository.ready_for_device(
            device_id=device_id,
            target_date="2026-08-03",
            config_version=int(prepared["schedule"]["config_version"]),
        )


def test_offline_day_preparation_rolls_back_when_a_release_is_invalid(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線失敗",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00", "20:00"],
    )
    first = _release(app, "offline-valid")
    repository = app.extensions["inktime_offline_schedule_repository"]

    try:
        repository.prepare_day(
            device_id=device_id,
            target_date="2026-08-03",
            release_ids=[first["release_id"], "missing-release"],
        )
    except ValueError as error:
        assert "Release" in str(error)
    else:
        raise AssertionError("invalid offline schedule unexpectedly committed")

    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_offline_schedules WHERE device_id=?", (device_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_items WHERE device_id=?", (device_id,)
        ).fetchone()[0] == 0


def test_offline_prepare_rejects_invalid_dates_config_and_snapshot_inputs(app):
    repository = app.extensions["inktime_offline_schedule_repository"]
    devices = app.extensions["inktime_device_repository"]
    device_id, _token = devices.create(
        "離線輸入邊界",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    first = _release(app, "offline-boundary-first")
    second = _release(app, "offline-boundary-second")

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        repository.prepare_day(device_id=device_id, target_date="bad-date", release_ids=[first["release_id"]])
    with pytest.raises(ValueError, match="最多 12"):
        repository.prepare_day(device_id=device_id, target_date="2026-08-03", release_ids=[])
    with pytest.raises(ValueError, match="Release ID"):
        repository.prepare_day(device_id=device_id, target_date="2026-08-03", release_ids=["bad/id"])
    with pytest.raises(ValueError, match="不可重複"):
        repository.prepare_day(
            device_id=device_id,
            target_date="2026-08-03",
            release_ids=[first["release_id"], first["release_id"]],
        )

    with app.extensions["inktime_database"].session() as connection:
        config_version = int(
            connection.execute("SELECT config_version FROM devices WHERE id=?", (device_id,)).fetchone()[0]
        )
        connection.execute("UPDATE devices SET schedule_times_json='not-json' WHERE id=?", (device_id,))
    with pytest.raises(ValueError, match="設定已變更"):
        repository.prepare_day(
            device_id=device_id,
            target_date="2026-08-03",
            release_ids=[first["release_id"]],
            expected_config_version=config_version + 1,
        )
    with pytest.raises(ValueError, match="schedule_times"):
        repository.prepare_day(device_id=device_id, target_date="2026-08-03", release_ids=[first["release_id"]])

    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE devices SET schedule_times_json=? WHERE id=?", ('["08:00"]', device_id))
    with pytest.raises(ValueError, match="數量"):
        repository.prepare_day(
            device_id=device_id,
            target_date="2026-08-03",
            release_ids=[first["release_id"], second["release_id"]],
        )

    online_id, _online_token = devices.create("非離線裝置")
    with pytest.raises(ValueError, match="未啟用離線"):
        repository.prepare_day(device_id=online_id, target_date="2026-08-03", release_ids=[first["release_id"]])

    with pytest.raises(KeyError):
        repository.prepare_day(device_id="missing-device", target_date="2026-08-03", release_ids=[first["release_id"]])
