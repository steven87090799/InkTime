from __future__ import annotations

from PIL import Image

from tests.conftest import create_admin, csrf, login


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
        == 400
    )


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
