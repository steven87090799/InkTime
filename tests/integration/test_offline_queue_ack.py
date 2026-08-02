from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PIL import Image


def _release(app) -> dict:
    staged = app.extensions["inktime_release_publisher"].publish(
        [("offline-ack", Image.new("RGB", (480, 800), "white"))],
        profile_key="safe_4c",
        activate=False,
    )
    return app.extensions["inktime_release_coordinator"].publish(
        [staged], created_by="offline-ack-test", photo_ids=[]
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
    release = _release(app)
    queue = app.extensions["inktime_resilience_repository"]
    queue.ensure_queue(device_id)
    item = queue.enqueue_release(
        device_id=device_id,
        release_id=release["release_id"],
        delivery_mode="offline_schedule",
        offline_prefetch_allowed=True,
        offline_slot="08:00",
        ack_deadline=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )
    manifest = client.get(
        "/api/device/v1/queue/manifest",
        headers={"Authorization": f"Bearer {token}"},
    ).get_json()
    version = manifest["queue_version"]
    for index, event in enumerate(("MANIFEST_RECEIVED", "DOWNLOAD_COMPLETED", "HASH_VERIFIED")):
        assert _ack(client, token, item["id"], version, event, f"prepare-{index}").status_code == 200

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
    delayed = _ack(
        client,
        token,
        item["id"],
        version - 1,
        "DISPLAY_COMPLETED",
        "delayed-completed",
        ack_mode="delayed_terminal",
        release_id=release["release_id"],
    )
    assert delayed.status_code == 200

    ordinary, ordinary_token = app.extensions["inktime_device_repository"].create("一般 ACK")
    ordinary_release = _release(app)
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
