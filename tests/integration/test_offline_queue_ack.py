from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from PIL import Image
import pytest


def _release(app, source_photo_id: str = "offline-ack") -> dict:
    staged = app.extensions["inktime_release_publisher"].publish(
        [(source_photo_id, Image.new("RGB", (480, 800), "white"))],
        profile_key="safe_4c",
        activate=False,
    )
    return app.extensions["inktime_release_coordinator"].publish(
        [staged], created_by="offline-ack-test", photo_ids=[source_photo_id]
    )[0]


def _ack(client, token: str, item_id: str, version: int, event: str, key: str, **extra):
    return client.post(
        "/api/device/v1/queue/ack",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "queue_item_id": item_id,
            "queue_version": version,
            "event": event,
            "idempotency_key": key,
            **extra,
        },
    )


def test_delayed_terminal_ack_is_only_allowed_for_prefetched_offline_item(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create(
        "離線 ACK",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    photo_id = "offline-ack-photo"
    library_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
            (library_id, "ACK 測試照片", ".", now, now),
        )
        connection.execute(
            "INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (photo_id, library_id, "offline-ack.jpg", "analyzed", now, now),
        )
    release = _release(app, photo_id)
    queue = app.extensions["inktime_resilience_repository"]
    schedule_repository = app.extensions["inktime_offline_schedule_repository"]
    schedule_repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-03",
        release_ids=[release["release_id"]],
    )
    with app.extensions["inktime_database"].session() as connection:
        item = connection.execute(
            "SELECT * FROM device_content_queue_items WHERE device_id=? ORDER BY position LIMIT 1",
            (device_id,),
        ).fetchone()
        connection.execute(
            "UPDATE device_content_queue_items SET ack_deadline=?,terminal_ack_retention=? WHERE id=?",
            (
                (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                item["id"],
            ),
        )
    item = dict(item)
    manifest = client.get(
        "/api/device/v1/queue/manifest",
        headers={"Authorization": f"Bearer {token}"},
    ).get_json()
    version = manifest["queue_version"]
    for index, event in enumerate(("MANIFEST_RECEIVED", "DOWNLOAD_COMPLETED", "HASH_VERIFIED")):
        assert _ack(client, token, item["id"], version, event, f"prepare-{index}").status_code == 200

    newer_release = _release(app, "offline-ack-newer")
    schedule_repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-04",
        release_ids=[newer_release["release_id"]],
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE device_content_queues SET current_release_id=?,last_known_good_release_id=? WHERE device_id=?",
            (newer_release["release_id"], newer_release["release_id"], device_id),
        )

    delayed_started = _ack(
        client,
        token,
        item["id"],
        version - 1,
        "DISPLAY_STARTED",
        "delayed-start",
        ack_mode="delayed_terminal",
        release_id=release["release_id"],
    )
    assert delayed_started.status_code == 400
    completed_epoch = int(datetime.now(timezone.utc).timestamp())
    delayed = _ack(
        client,
        token,
        item["id"],
        version - 1,
        "DISPLAY_COMPLETED",
        "delayed-completed",
        ack_mode="delayed_terminal",
        release_id=release["release_id"],
        event_epoch=completed_epoch,
    )
    assert delayed.status_code == 200
    replay = _ack(
        client,
        token,
        item["id"],
        version - 1,
        "DISPLAY_COMPLETED",
        "delayed-completed",
        ack_mode="delayed_terminal",
        release_id=release["release_id"],
    )
    assert replay.status_code == 200
    with app.extensions["inktime_database"].session() as connection:
        queue_head = connection.execute(
            "SELECT current_release_id FROM device_content_queues WHERE device_id=?", (device_id,)
        ).fetchone()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_events WHERE queue_item_id=? AND event_type='DISPLAY_COMPLETED'",
            (item["id"],),
        ).fetchone()[0]
        history_count = connection.execute(
            "SELECT COUNT(*) FROM display_history WHERE release_id=? AND selection_method='device_queue_ack'",
            (release["release_id"],),
        ).fetchone()[0]
        history = connection.execute(
            "SELECT history_date,displayed_at,metadata_json FROM display_history WHERE release_id=? AND selection_method='device_queue_ack'",
            (release["release_id"],),
        ).fetchone()
    assert queue_head["current_release_id"] == newer_release["release_id"]
    assert event_count == 1
    assert history_count == 1
    assert history["history_date"] == "2026-08-03"
    assert history["displayed_at"] == datetime.fromtimestamp(completed_epoch, timezone.utc).isoformat()
    assert json.loads(history["metadata_json"])["timestamp_source"] == "device_event"

    failed_device, failed_token = app.extensions["inktime_device_repository"].create(
        "離線失敗 ACK",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    failed_release = _release(app, "offline-failed-ack")
    schedule_repository.prepare_day(
        device_id=failed_device,
        target_date="2026-08-03",
        release_ids=[failed_release["release_id"]],
    )
    with app.extensions["inktime_database"].session() as connection:
        failed_item = dict(
            connection.execute(
                "SELECT * FROM device_content_queue_items WHERE device_id=?",
                (failed_device,),
            ).fetchone()
        )
        failed_version = int(
            connection.execute(
                "SELECT queue_version FROM device_content_queues WHERE device_id=?",
                (failed_device,),
            ).fetchone()[0]
        )
    failed_token_epoch = int(datetime.now(timezone.utc).timestamp())
    for index, event in enumerate(("MANIFEST_RECEIVED", "DOWNLOAD_COMPLETED", "HASH_VERIFIED")):
        assert _ack(
            client,
            failed_token,
            failed_item["id"],
            failed_version,
            event,
            f"failed-prepare-{index}",
        ).status_code == 200
    failed = _ack(
        client,
        failed_token,
        failed_item["id"],
        failed_version - 1,
        "DISPLAY_FAILED",
        "delayed-failed",
        ack_mode="delayed_terminal",
        release_id=failed_release["release_id"],
        event_epoch=failed_token_epoch,
        error_code="DISPLAY-TIMEOUT",
    )
    failed_replay = _ack(
        client,
        failed_token,
        failed_item["id"],
        failed_version - 1,
        "DISPLAY_FAILED",
        "delayed-failed",
        ack_mode="delayed_terminal",
        release_id=failed_release["release_id"],
        event_epoch=failed_token_epoch,
        error_code="DISPLAY-TIMEOUT",
    )
    assert failed.status_code == 200
    assert failed_replay.status_code == 200
    assert failed_replay.get_json()["idempotent"] is True
    with app.extensions["inktime_database"].session() as connection:
        failed_state = connection.execute(
            "SELECT status,COUNT(*) OVER () AS _count FROM device_content_queue_items WHERE id=?",
            (failed_item["id"],),
        ).fetchone()
        failed_event_count = connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_events WHERE queue_item_id=? AND event_type='DISPLAY_FAILED'",
            (failed_item["id"],),
        ).fetchone()[0]
    assert failed_state["status"] == "FAILED"
    assert failed_event_count == 1

    ordinary, ordinary_token = app.extensions["inktime_device_repository"].create("一般 ACK")
    ordinary_release = _release(app, "ordinary-ack")
    queue.ensure_queue(ordinary)
    ordinary_item = queue.enqueue_release(device_id=ordinary, release_id=ordinary_release["release_id"])
    ordinary_manifest = client.get(
        "/api/device/v1/queue/manifest",
        headers={"Authorization": f"Bearer {ordinary_token}"},
    ).get_json()
    ordinary_delayed = _ack(
        client,
        ordinary_token,
        ordinary_item["id"],
        ordinary_manifest["queue_version"] - 1,
        "DISPLAY_COMPLETED",
        "ordinary-delayed",
        ack_mode="delayed_terminal",
        release_id=ordinary_release["release_id"],
    )
    assert ordinary_delayed.status_code == 409


def test_queue_enqueue_requires_explicit_offline_schedule_ownership(app):
    queue = app.extensions["inktime_resilience_repository"]
    online_device, _online_token = app.extensions["inktime_device_repository"].create("online scope")
    release = _release(app, "queue-scope")
    queue.ensure_queue(online_device)

    with pytest.raises(ValueError, match="delivery_mode"):
        queue.enqueue_release(
            device_id=online_device,
            release_id=release["release_id"],
            delivery_mode="invalid",
        )
    with pytest.raises(ValueError, match="offline_prefetch_allowed"):
        queue.enqueue_release(
            device_id=online_device,
            release_id=release["release_id"],
            delivery_mode="online_queue",
            offline_prefetch_allowed=True,
        )
    with pytest.raises(ValueError, match="offline_schedule_id"):
        queue.enqueue_release(
            device_id=online_device,
            release_id=release["release_id"],
            delivery_mode="online_queue",
            offline_schedule_id="not-allowed",
        )

    offline_device, _offline_token = app.extensions["inktime_device_repository"].create(
        "offline scope",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    queue.ensure_queue(offline_device)
    with pytest.raises(ValueError, match="必須綁定 offline_schedule_id"):
        queue.enqueue_release(
            device_id=offline_device,
            release_id=release["release_id"],
            delivery_mode="offline_schedule",
            offline_prefetch_allowed=True,
        )
    with pytest.raises(ValueError, match="不屬於此裝置"):
        queue.enqueue_release(
            device_id=offline_device,
            release_id=release["release_id"],
            delivery_mode="offline_schedule",
            offline_prefetch_allowed=True,
            offline_schedule_id="wrong-owner",
        )

    schedule = app.extensions["inktime_offline_schedule_repository"].prepare_day(
        device_id=offline_device,
        target_date="2026-08-03",
        release_ids=[release["release_id"]],
    )
    duplicate = queue.enqueue_release(
        device_id=offline_device,
        release_id=release["release_id"],
        delivery_mode="offline_schedule",
        offline_prefetch_allowed=True,
        offline_schedule_id=schedule["schedule"]["id"],
    )
    assert duplicate["offline_schedule_id"] == schedule["schedule"]["id"]
