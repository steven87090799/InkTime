from __future__ import annotations

from PIL import Image


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

    assert prepared["schedule"]["status"] == "ready"
    assert len(prepared["slots"]) == 2
    assert all(len(slot["sha256"]) == 64 for slot in prepared["slots"])
    assert all(slot["show_at"].endswith("+00:00") for slot in prepared["slots"])
    with app.extensions["inktime_database"].session() as connection:
        queue_items = connection.execute(
            """
            SELECT delivery_mode,offline_prefetch_allowed,offline_slot
            FROM device_content_queue_items WHERE device_id=? ORDER BY position
            """,
            (device_id,),
        ).fetchall()
    assert [(row["delivery_mode"], row["offline_prefetch_allowed"]) for row in queue_items] == [
        ("offline_schedule", 1),
        ("offline_schedule", 1),
    ]

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
    assert body["schedule_times"] == ["08:00", "20:00"]
    assert body["prefetch_lead_minutes"] == 5
    assert body["slots"][0]["sha256"] == prepared["slots"][0]["sha256"]
    assert body["slots"][0]["show_at"].endswith("+08:00")
    assert body["slots"][0]["download_url"].startswith("/api/device/v1/queue/items/")
    assert body["slots"][0]["size"] == 96_000

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
    assert [row["status"] for row in statuses] == ["CANCELLED", "CANCELLED", "READY", "READY"]


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
