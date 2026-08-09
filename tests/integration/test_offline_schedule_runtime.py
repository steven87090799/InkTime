from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from PIL import Image
import pytest

from tests.conftest import csrf, create_admin, login


def _release(app, name: str) -> dict:
    staged = app.extensions["inktime_release_publisher"].publish(
        [(name, Image.new("RGB", (480, 800), "white"))],
        profile_key="safe_4c",
        activate=False,
    )
    return app.extensions["inktime_release_coordinator"].publish(
        [staged], created_by="offline-test", photo_ids=[]
    )[0]


def _device_dates() -> tuple[str, str]:
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    return today.isoformat(), (today + timedelta(days=1)).isoformat()


def test_offline_day_preparation_is_atomic_and_device_projection_contains_full_sha(client, app):
    today, tomorrow = _device_dates()
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
        target_date=today,
        release_ids=[first["release_id"], second["release_id"]],
    )
    same_day = repository.prepare_day(
        device_id=device_id,
        target_date=today,
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
    assert body["target_local_date"] == today
    assert body["panel_profile"] == "safe_4c"
    assert body["rotation"] == 0
    assert body["target_start_epoch"] < body["slots"][0]["show_at_epoch"] < body["target_end_epoch"]
    assert body["target"] == "current"
    slot_zone = datetime.fromisoformat(body["slots"][0]["show_at"]).tzinfo
    assert body["next_target_start_epoch"] == int(
        datetime.combine(datetime.fromisoformat(tomorrow).date(), time.min, tzinfo=slot_zone).timestamp()
    )
    assert body["next_target_start_epoch"] < body["next_schedule_prefetch_epoch"] < int(
        datetime.combine(datetime.fromisoformat(tomorrow).date(), time(8, 0), tzinfo=slot_zone).timestamp()
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
        target_date=tomorrow,
        release_ids=[third["release_id"], fourth["release_id"]],
    )
    assert next_day["schedule"]["target_date"] == tomorrow
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
    assert still_today.get_json()["target_local_date"] == today
    next_response = client.get(
        "/api/device/v1/offline-schedule?target=next",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert next_response.status_code == 200
    next_body = next_response.get_json()
    assert next_body["target"] == "next"
    assert next_body["target_local_date"] == tomorrow
    assert next_body["schedule_id"] == next_day["schedule"]["id"]
    invalid_date = (datetime.fromisoformat(tomorrow).date() + timedelta(days=1)).isoformat()
    for invalid_target in (invalid_date, "+1", "history"):
        assert client.get(
            f"/api/device/v1/offline-schedule?target={invalid_target}",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code == 400


def test_prepare_day_recovers_terminal_shortage_and_clears_outcome(app):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "恢復離線終端結果的相框",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    release = _release(app, "offline-recovered-shortage")
    with app.extensions["inktime_database"].session() as connection:
        config_version = int(
            connection.execute(
                "SELECT config_version FROM devices WHERE id=?", (device_id,)
            ).fetchone()[0]
        )
    repository = app.extensions["inktime_offline_schedule_repository"]
    terminal = repository.record_terminal_outcome(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=config_version,
        outcome_code="NO_ELIGIBLE_CANDIDATES",
        message="測試中的候選不足",
    )
    assert terminal["status"] == "completed"

    prepared = repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-03",
        release_ids=[release["release_id"]],
    )

    assert prepared["schedule"]["status"] == "ready"
    with app.extensions["inktime_database"].session() as connection:
        schedule = connection.execute(
            """
            SELECT status,terminal_outcome_code
            FROM device_offline_schedules
            WHERE device_id=? AND target_date=? AND config_version=?
            """,
            (device_id, "2026-08-03", config_version),
        ).fetchone()
    assert schedule["status"] == "ready"
    assert schedule["terminal_outcome_code"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("frame_orientation", "landscape"),
        ("layout_mode", "adaptive_memory"),
        ("fit_mode", "cover"),
        ("panel_profile", "gdep073e01_6c"),
    ],
)
def test_render_input_change_supersedes_future_playlist_and_rejects_stale_prepare(
    app, field, value
):
    devices = app.extensions["inktime_device_repository"]
    device_id, _token = devices.create(
        "render version guard",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    release = _release(app, f"render-version-old-{field}")
    target = (datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)).isoformat()
    repository = app.extensions["inktime_offline_schedule_repository"]
    prepared = repository.prepare_day(
        device_id=device_id,
        target_date=target,
        release_ids=[release["release_id"]],
    )
    old_config_version = int(prepared["schedule"]["config_version"])
    old_offline_version = int(devices.get(device_id)["offline_schedule_version"])

    devices.update_render_inputs(device_id, **{field: value})

    updated = devices.get(device_id)
    assert int(updated["config_version"]) == old_config_version + 1
    assert int(updated["offline_schedule_version"]) == old_offline_version
    assert updated[field] == value
    assert repository.ready_for_device(
        device_id=device_id,
        target_date=target,
        config_version=int(updated["config_version"]),
    ) is None
    with app.extensions["inktime_database"].session() as connection:
        queue_status = connection.execute(
            "SELECT status FROM device_content_queue_items WHERE device_id=?",
            (device_id,),
        ).fetchone()["status"]
        historical = connection.execute(
            "SELECT status FROM device_offline_schedules WHERE id=?",
            (prepared["schedule"]["id"],),
        ).fetchone()["status"]
    assert queue_status == "CANCELLED"
    assert historical == "ready"
    with pytest.raises(ValueError, match="設定已變更"):
        repository.prepare_day(
            device_id=device_id,
            target_date=target,
            release_ids=[release["release_id"]],
            expected_config_version=old_config_version,
        )


def test_actual_playlist_preview_reads_committed_schedule_without_reselection(app, monkeypatch):
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線實際 Playlist 預覽",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    release = _release(app, "offline-preview-committed")
    target = datetime(2026, 8, 3).date()
    prepared = app.extensions["inktime_offline_schedule_repository"].prepare_day(
        device_id=device_id,
        target_date=target.isoformat(),
        release_ids=[release["release_id"]],
    )

    def fail_if_selection_runs(*_args, **_kwargs):
        raise AssertionError("committed playlist preview must not re-run selection")

    monkeypatch.setattr(
        app.extensions["inktime_render_service"],
        "select_candidates_details",
        fail_if_selection_runs,
    )
    preview = app.extensions["inktime_display_preparation_service"].preview(
        {
            "display_times": ["08:00"],
            "daily_count": 1,
            "device_ids": [device_id],
            "candidate_years": [],
            "prefetch_count": 1,
            "ai_fallback": "use_existing",
            "render_fallback": "keep_current",
        },
        target_date=target,
    )
    assert preview["outcome"] == "ready"
    assert preview["device_id"] == device_id
    assert preview["target_date"] == target.isoformat()
    assert preview["config_version"] == prepared["schedule"]["config_version"]
    assert preview["playlist_version"] == prepared["playlist_version"]
    assert preview["playlist"][0]["release_id"] == release["release_id"]
    assert preview["playlist"][0]["show_at"] == prepared["slots"][0]["show_at"]


def test_actual_playlist_preview_defaults_to_device_local_date(app, monkeypatch):
    app.extensions["inktime_settings_repository"].update(
        "general.timezone", "Pacific/Honolulu", changed_by="test", source_ip="127.0.0.1"
    )
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "跨時區離線實際 Playlist 預覽",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
        timezone_name="Pacific/Kiritimati",
    )
    release = _release(app, "offline-preview-device-local-date")
    target = "2026-08-04"
    prepared = app.extensions["inktime_offline_schedule_repository"].prepare_day(
        device_id=device_id,
        target_date=target,
        release_ids=[release["release_id"]],
    )
    seen_timezones = []

    def device_local_date(timezone_name: str) -> date:
        seen_timezones.append(timezone_name)
        return date(2026, 8, 4)

    monkeypatch.setattr(
        "inktime.app.services.display_prepare.current_local_date", device_local_date
    )
    preview = app.extensions["inktime_display_preparation_service"].preview(
        {
            "display_times": ["08:00"],
            "daily_count": 1,
            "device_ids": [device_id],
            "candidate_years": [],
            "prefetch_count": 1,
            "ai_fallback": "use_existing",
            "render_fallback": "keep_current",
        }
    )

    assert preview["outcome"] == "ready"
    assert preview["target_date"] == target
    assert preview["playlist_version"] == prepared["playlist_version"]
    assert preview["playlist"][0]["release_id"] == release["release_id"]
    assert seen_timezones == ["Pacific/Kiritimati"]


def test_actual_playlist_preview_rejects_device_outside_saved_schedule(app, monkeypatch):
    devices = app.extensions["inktime_device_repository"]
    device_a, _token_a = devices.create(
        "預覽排程裝置 A",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    device_b, _token_b = devices.create(
        "預覽排程裝置 B",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    device_c, _token_c = devices.create(
        "未加入排程裝置 C",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )

    def fail_if_committed_playlist_is_read(*_args, **_kwargs):
        raise AssertionError("invalid preview device must be rejected before committed Playlist read")

    monkeypatch.setattr(
        app.extensions["inktime_offline_schedule_repository"],
        "ready_for_device",
        fail_if_committed_playlist_is_read,
    )
    with pytest.raises(ValueError, match="不屬於此 display_prepare 排程"):
        app.extensions["inktime_display_preparation_service"].preview(
            {
                "display_times": ["08:00"],
                "daily_count": 1,
                "device_ids": [device_a, device_b],
                "candidate_years": [],
                "prefetch_count": 1,
                "ai_fallback": "use_existing",
                "render_fallback": "keep_current",
            },
            device_id=device_c,
        )


def test_offline_slot_replacement_rejects_historical_release_and_preserves_state(client, app):
    create_admin(app)
    login(client)
    devices = app.extensions["inktime_device_repository"]
    device_id, _token = devices.create(
        "離線 Slot replacement 邊界",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    historical = _release(app, "offline-historical-release")
    current = _release(app, "offline-current-release")
    fresh = _release(app, "offline-fresh-release")
    repository = app.extensions["inktime_offline_schedule_repository"]

    repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-02",
        release_ids=[historical["release_id"]],
    )
    with app.extensions["inktime_database"].session() as connection:
        historical_item = connection.execute(
            """
            SELECT id FROM device_content_queue_items
            WHERE device_id=? AND release_id=?
            """,
            (device_id, historical["release_id"]),
        ).fetchone()
        connection.execute(
            "UPDATE device_content_queue_items SET status='DISPLAYED' WHERE id=?",
            (historical_item["id"],),
        )

    prepared = repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-03",
        release_ids=[current["release_id"]],
    )
    schedule_id = prepared["schedule"]["id"]
    before_playlist_version = prepared["playlist_version"]
    with app.extensions["inktime_database"].session() as connection:
        queue_item_id = connection.execute(
            "SELECT queue_item_id FROM device_offline_schedule_slots WHERE schedule_id=? AND slot_index=0",
            (schedule_id,),
        ).fetchone()["queue_item_id"]
        connection.execute(
            "UPDATE device_content_queue_items SET display_after=? WHERE id=?",
            ((datetime.now(ZoneInfo("UTC")) + timedelta(hours=1)).isoformat(), queue_item_id),
        )
        before_queue_version = int(
            connection.execute(
                "SELECT queue_version FROM device_content_queues WHERE device_id=?", (device_id,)
            ).fetchone()["queue_version"]
        )

    with pytest.raises(ValueError, match="QUEUE-005"):
        repository.replace_slot(
            device_id=device_id,
            schedule_id=schedule_id,
            slot_index=0,
            release_id=historical["release_id"],
        )

    unchanged = repository.ready_for_device(
        device_id=device_id,
        target_date="2026-08-03",
        config_version=int(prepared["schedule"]["config_version"]),
    )
    assert unchanged["slots"][0]["release_id"] == current["release_id"]
    assert unchanged["playlist_version"] == before_playlist_version
    with app.extensions["inktime_database"].session() as connection:
        assert int(
            connection.execute(
                "SELECT queue_version FROM device_content_queues WHERE device_id=?", (device_id,)
            ).fetchone()["queue_version"]
        ) == before_queue_version

    response = client.post(
        f"/api/v1/devices/{device_id}/offline-schedule/{schedule_id}/slots/0",
        json={"release_id": historical["release_id"]},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 409

    replaced = repository.replace_slot(
        device_id=device_id,
        schedule_id=schedule_id,
        slot_index=0,
        release_id=fresh["release_id"],
    )
    assert replaced["slots"][0]["release_id"] == fresh["release_id"]
    assert replaced["playlist_version"] != before_playlist_version
    with app.extensions["inktime_database"].session() as connection:
        assert int(
            connection.execute(
                "SELECT queue_version FROM device_content_queues WHERE device_id=?", (device_id,)
            ).fetchone()["queue_version"]
        ) == before_queue_version + 1


def test_offline_prepare_rejects_historical_release_and_preserves_state(client, app):
    create_admin(app)
    login(client)
    device_id, _token = app.extensions["inktime_device_repository"].create(
        "離線 prepare historical Release 邊界",
        delivery_mode="inktime_offline_schedule",
        offline_prefetch_allowed=True,
        schedule_times=["08:00"],
    )
    historical = _release(app, "offline-prepare-historical-release")
    fresh = _release(app, "offline-prepare-fresh-release")
    repository = app.extensions["inktime_offline_schedule_repository"]

    repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-02",
        release_ids=[historical["release_id"]],
    )
    with app.extensions["inktime_database"].session() as connection:
        historical_item = connection.execute(
            """
            SELECT id FROM device_content_queue_items
            WHERE device_id=? AND release_id=?
            """,
            (device_id, historical["release_id"]),
        ).fetchone()
        connection.execute(
            "UPDATE device_content_queue_items SET status='DISPLAYED' WHERE id=?",
            (historical_item["id"],),
        )
        before_queue_version = int(
            connection.execute(
                "SELECT queue_version FROM device_content_queues WHERE device_id=?", (device_id,)
            ).fetchone()["queue_version"]
        )

    response = client.post(
        f"/api/v1/devices/{device_id}/offline-schedule/prepare",
        json={"target_date": "2026-08-03", "release_ids": [historical["release_id"]]},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 400
    body = response.get_json()
    assert body["error_code"] == "QUEUE-005"
    assert body["message"] == "Release 已存在於裝置 Queue 歷史，不可重複使用"

    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_offline_schedules WHERE device_id=? AND target_date=?",
            (device_id, "2026-08-03"),
        ).fetchone()[0] == 0
        assert connection.execute(
            """
            SELECT COUNT(*) FROM device_offline_schedule_slots s
            JOIN device_offline_schedules d ON d.id=s.schedule_id
            WHERE d.device_id=? AND d.target_date=?
            """,
            (device_id, "2026-08-03"),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_items WHERE device_id=?", (device_id,)
        ).fetchone()[0] == 1
        assert int(
            connection.execute(
                "SELECT queue_version FROM device_content_queues WHERE device_id=?", (device_id,)
            ).fetchone()["queue_version"]
        ) == before_queue_version

    recovered = repository.prepare_day(
        device_id=device_id,
        target_date="2026-08-03",
        release_ids=[fresh["release_id"]],
    )
    assert recovered["schedule"]["status"] == "ready"
    assert len(recovered["slots"]) == 1
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM device_content_queue_items WHERE device_id=?", (device_id,)
        ).fetchone()[0] == 2


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
    today, tomorrow = _device_dates()
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
    assert body["target_date"] == today
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
    assert next_body["target_date"] == tomorrow
    assert next_body["retry_after_epoch"] < int(
        datetime.combine(datetime.fromisoformat(tomorrow).date(), time(8, 0), tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
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
    with pytest.raises(ValueError, match="最多 24"):
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
