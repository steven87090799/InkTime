from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PIL import Image
import pytest

from tests.conftest import create_admin, csrf, login
from inktime.app.services.budgets import BudgetExceeded


def _seed_photo(app, photo_id: str = "photo") -> None:
    database = app.extensions["inktime_database"]
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES ('library','測試','/tmp',datetime('now'),datetime('now'))"
        )
        connection.execute(
            "INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at) VALUES (?, 'library', 'photo.jpg', 'analyzed',datetime('now'),datetime('now'))",
            (photo_id,),
        )


def _published_release(app, photo_id: str = "photo") -> dict:
    manifest = app.extensions["inktime_release_publisher"].publish(
        [(photo_id, Image.new("RGB", (480, 800), "white"))], profile_key="safe_4c", activate=False
    )
    return app.extensions["inktime_release_coordinator"].publish([manifest], created_by="test", photo_ids=[])[
        0
    ]


def _ack(client, token: str, item_id: str, version: int, event: str, key: str):
    return client.post(
        "/api/device/queue/ack",
        headers={"Authorization": f"Bearer {token}"},
        json={"queue_item_id": item_id, "queue_version": version, "event": event, "idempotency_key": key},
    )


def test_queue_manifest_download_ack_is_owned_idempotent_and_updates_history(client, app):
    create_admin(app)
    login(client)
    _seed_photo(app)
    device_id, token = app.extensions["inktime_device_repository"].create("裝置 A")
    other_id, other_token = app.extensions["inktime_device_repository"].create("裝置 B")
    release = _published_release(app)
    generated = client.post(
        f"/api/devices/{device_id}/queue/generate",
        json={"release_id": release["release_id"], "depth": 3},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert generated.status_code == 201
    manifest = client.get(
        "/api/device/v1/queue/manifest", headers={"Authorization": f"Bearer {token}"}
    ).get_json()
    item = manifest["items"][0]
    assert item["width"] == 480
    assert item["height"] == 800
    assert item["pixel_format"] == "2bpp"
    assert item["render_profile"] == "safe_4c"
    assert (
        client.get(item["download_url"], headers={"Authorization": f"Bearer {other_token}"}).status_code
        == 403
    )
    assert client.get(item["download_url"], headers={"Authorization": f"Bearer {token}"}).status_code == 200
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM display_history").fetchone()[0] == 0
    for index, event in enumerate(
        ("MANIFEST_RECEIVED", "DOWNLOAD_STARTED", "DOWNLOAD_COMPLETED", "HASH_VERIFIED", "DISPLAY_STARTED")
    ):
        assert (
            _ack(
                client, token, item["queue_item_id"], manifest["queue_version"], event, f"event-{index}"
            ).status_code
            == 200
        )
    assert (
        _ack(
            client, token, item["queue_item_id"], manifest["queue_version"], "DISPLAY_COMPLETED", "displayed"
        ).status_code
        == 200
    )
    duplicate = _ack(
        client, token, item["queue_item_id"], manifest["queue_version"], "DISPLAY_COMPLETED", "displayed"
    )
    assert duplicate.status_code == 200 and duplicate.get_json()["idempotent"] is True
    with app.extensions["inktime_database"].session() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM display_history WHERE selection_method='device_queue_ack'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT last_known_good_release_id FROM device_content_queues WHERE device_id=?", (device_id,)
            ).fetchone()[0]
            == release["release_id"]
        )
    assert (
        _ack(
            client,
            other_token,
            item["queue_item_id"],
            manifest["queue_version"],
            "DISPLAY_COMPLETED",
            "forged",
        ).status_code
        == 403
    )
    assert other_id != device_id


def test_generic_queue_manifest_is_bounded_and_excludes_offline_schedule_rows(client, app):
    device_id, token = app.extensions["inktime_device_repository"].create("Queue bound")
    queue = app.extensions["inktime_resilience_repository"]
    queue.ensure_queue(device_id, depth=14)
    for index in range(14):
        release = _published_release(app, f"queue-bound-{index}")
        queue.enqueue_release(device_id=device_id, release_id=release["release_id"])

    manifest = client.get(
        "/api/device/v1/queue/manifest", headers={"Authorization": f"Bearer {token}"}
    ).get_json()

    assert len(manifest["items"]) == 14
    assert all(item["delivery_mode"] == "online_queue" for item in manifest["items"])


def test_enhanced_offline_queue_accepts_24_slot_depth(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "24-slot offline queue",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=[f"{hour:02d}:00" for hour in range(24)],
        offline_schedule_max_slots=24,
    )
    queue = app.extensions["inktime_resilience_repository"]

    result = queue.ensure_queue(device_id, depth=24)

    assert result["queue"]["depth"] == 24


def test_queue_rejects_stale_version_and_illegal_transition(client, app):
    create_admin(app)
    login(client)
    _seed_photo(app)
    device_id, token = app.extensions["inktime_device_repository"].create("裝置 A")
    release = _published_release(app)
    response = client.post(
        f"/api/devices/{device_id}/queue/generate",
        json={"release_id": release["release_id"]},
        headers={"X-CSRF-Token": csrf(client)},
    )
    item = response.get_json()["item"]
    manifest = client.get(
        "/api/device/v1/queue/manifest", headers={"Authorization": f"Bearer {token}"}
    ).get_json()
    assert (
        _ack(
            client, token, item["id"], manifest["queue_version"], "DISPLAY_COMPLETED", "out-of-order"
        ).status_code
        == 400
    )
    assert (
        _ack(
            client, token, item["id"], manifest["queue_version"] - 1, "MANIFEST_RECEIVED", "old-version"
        ).status_code
        == 409
    )


def test_same_content_queue_ack_is_strict_and_idempotent(client, app):
    create_admin(app)
    login(client)
    _seed_photo(app)
    device_id, token = app.extensions["inktime_device_repository"].create("Same content")
    release = _published_release(app)
    created = client.post(
        f"/api/devices/{device_id}/queue/generate",
        json={"release_id": release["release_id"]},
        headers={"X-CSRF-Token": csrf(client)},
    )
    item = created.get_json()["item"]
    manifest = client.get(
        "/api/device/v1/queue/manifest", headers={"Authorization": f"Bearer {token}"}
    ).get_json()
    for index, event in enumerate(
        ("MANIFEST_RECEIVED", "DOWNLOAD_STARTED", "DOWNLOAD_COMPLETED", "HASH_VERIFIED")
    ):
        assert (
            _ack(client, token, item["id"], manifest["queue_version"], event, f"skip-{index}").status_code
            == 200
        )
    payload = {
        "queue_item_id": item["id"],
        "queue_version": manifest["queue_version"],
        "event": "DISPLAY_COMPLETED",
        "idempotency_key": "same-content",
        "display_skipped": True,
        "skip_reason": "same_sha256",
    }
    first = client.post(
        "/api/device/v1/queue/ack",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    duplicate = client.post(
        "/api/device/v1/queue/ack",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 200
    assert duplicate.status_code == 200 and duplicate.get_json()["idempotent"] is True


def test_api_usage_retention_policy_is_exposed_and_preserves_current_budget_evidence(client, app):
    create_admin(app)
    login(client)
    _seed_photo(app, "retention-photo")
    database = app.extensions["inktime_database"]
    initial = client.get("/api/retention/policies")
    assert initial.status_code == 200
    api_usage = next(item for item in initial.get_json()["items"] if item["data_type"] == "api_usage")
    assert api_usage["retention_days"] == 400

    updated = client.put(
        "/api/retention/policies/api_usage",
        json={"retention_days": 1, "cleanup_batch_size": 2},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert updated.status_code == 200
    assert updated.get_json()["retention_days"] == 1
    costs_page = client.get("/costs")
    assert costs_page.status_code == 200
    assert "目前 API 用量保留期間（1 天）".encode() in costs_page.data

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with database.transaction() as connection:
        connection.executemany(
            "INSERT INTO api_usage(provider,model,photo_id,request_type,estimated_cost,started_at,status,cost_source,image_bytes) "
            "VALUES ('remote','model',?,'analysis',?,?,?,?,1)",
            [
                (
                    "retention-photo",
                    0.10,
                    (month_start - timedelta(days=3)).isoformat(),
                    "failed",
                    "unknown",
                ),
                (
                    "retention-photo",
                    0.20,
                    (now - timedelta(hours=1)).isoformat(),
                    "completed",
                    "unknown",
                ),
            ],
        )
        before_analysis = connection.execute("SELECT COUNT(*) FROM photo_analysis").fetchone()[0]
        before_queue_events = connection.execute("SELECT COUNT(*) FROM device_content_queue_events").fetchone()[0]

    budget = app.extensions["inktime_budget_service"]
    photos = app.extensions["inktime_photo_repository"]
    before = budget.snapshot(photo_id="retention-photo")
    assert before["photo_unknown_count"] == 2
    assert photos.ai_limit_reached(daily_limit=1, monthly_limit=1) is True

    run = client.post(
        "/api/retention/run",
        json={"dry_run": False},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert run.status_code == 200
    assert run.get_json()["summary"]["api_usage"] == 1

    after = budget.snapshot(photo_id="retention-photo")
    assert after["photo_unknown_count"] == 1
    assert photos.ai_limit_reached(daily_limit=1, monthly_limit=1) is True
    with pytest.raises(BudgetExceeded):
        budget.assert_request_allowed(None, "retention-photo")
    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM photo_analysis").fetchone()[0] == before_analysis
        assert connection.execute("SELECT COUNT(*) FROM device_content_queue_events").fetchone()[0] == before_queue_events


def test_queue_ack_rejects_string_skip_boolean(client, app):
    _device_id, token = app.extensions["inktime_device_repository"].create("Strict skip")
    response = client.post(
        "/api/device/v1/queue/ack",
        json={
            "queue_item_id": "item",
            "queue_version": 0,
            "event": "DISPLAY_COMPLETED",
            "idempotency_key": "key",
            "display_skipped": "true",
            "skip_reason": "same_sha256",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


def test_canary_failure_creates_last_known_good_rollback_queue(client, app):
    create_admin(app)
    login(client)
    _seed_photo(app)
    first_id, first_token = app.extensions["inktime_device_repository"].create("Canary A")
    second_id, second_token = app.extensions["inktime_device_repository"].create("Canary B")
    last_known_good = _published_release(app)
    canary_release = _published_release(app)
    with app.extensions["inktime_database"].transaction() as connection:
        for device_id in (first_id, second_id):
            connection.execute(
                "INSERT INTO device_content_queues(device_id,last_known_good_release_id,updated_at) VALUES (?,?,datetime('now'))",
                (device_id, last_known_good["release_id"]),
            )
    created = client.post(
        "/api/rollouts",
        json={
            "name": "回滾測試",
            "release_id": canary_release["release_id"],
            "stages": [{"target_percent": 100, "minimum_successful_devices": 2}],
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    rollout_id = created.get_json()["campaign"]["id"]
    started = client.post(
        f"/api/rollouts/{rollout_id}/start", json={}, headers={"X-CSRF-Token": csrf(client)}
    )
    assert started.status_code == 200
    targets = started.get_json()["targets"]
    tokens = {first_id: first_token, second_id: second_token}
    for target in targets:
        assert (
            _ack(
                client,
                tokens[target["device_id"]],
                target["queue_item_id"],
                0,
                "DISPLAY_FAILED",
                f"failed-{target['device_id']}",
            ).status_code
            == 200
        )
    rolling = client.get(f"/api/rollouts/{rollout_id}").get_json()
    assert rolling["campaign"]["status"] == "ROLLING_BACK"
    assert {target["status"] for target in rolling["targets"]} == {"rollback_pending"}
    with app.extensions["inktime_database"].session() as connection:
        rollback_items = connection.execute(
            "SELECT release_id,priority,status FROM device_content_queue_items WHERE device_id IN (?,?) AND release_id=?",
            (first_id, second_id, last_known_good["release_id"]),
        ).fetchall()
    assert {(row["release_id"], row["priority"], row["status"]) for row in rollback_items} == {
        (last_known_good["release_id"], 1000, "READY")
    }


def test_rollout_skips_enhanced_offline_devices_without_online_queue_items(client, app):
    create_admin(app)
    login(client)
    _seed_photo(app, "offline-rollout-photo")
    online_id, _online_token = app.extensions["inktime_device_repository"].create("Rollout online")
    offline_id, _offline_token = app.extensions["inktime_device_repository"].create(
        "Rollout offline",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    release = _published_release(app, "offline-rollout-photo")
    created = client.post(
        "/api/rollouts",
        json={
            "name": "模式隔離 rollout",
            "release_id": release["release_id"],
            "stages": [{"target_percent": 100, "minimum_successful_devices": 1}],
        },
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert created.status_code == 201
    rollout_id = created.get_json()["campaign"]["id"]
    started = client.post(
        f"/api/rollouts/{rollout_id}/start", json={}, headers={"X-CSRF-Token": csrf(client)}
    )
    assert started.status_code == 200
    targets = {target["device_id"]: target for target in started.get_json()["targets"]}
    assert targets[offline_id]["status"] == "skipped_incompatible_offline"
    assert targets[offline_id]["queue_item_id"] is None
    assert targets[online_id]["queue_item_id"]
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_items WHERE device_id=? AND delivery_mode='online_queue'",
            (offline_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM rollout_health_events WHERE rollout_id=? AND device_id=? AND error_code='ROLLOUT-005'",
            (rollout_id, offline_id),
        ).fetchone()[0] == 1
