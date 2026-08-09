from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from PIL import Image
import pytest

from inktime.app.domain.photopainter.offline_schedule import OFFLINE_PREPARE_BOOTSTRAP_AT
from inktime.app.domain.rendering.system_presets import SYSTEM_PRESETS
from inktime.app.repositories.settings import (
    DEVICE_OVERRIDE_KEYS,
    SETTING_DEFINITIONS,
)
from tests.conftest import create_admin, csrf, login


def _post(client, path: str, payload: dict, *, confirm: bool = False):
    headers = {"X-CSRF-Token": csrf(client)}
    if confirm:
        headers["X-InkTime-Confirm-Risk"] = "true"
    return client.post(
        path,
        json=payload,
        headers=headers,
    )


def _install_atomic_test_preset(monkeypatch, *, extra_settings: dict | None = None) -> str:
    key = "atomic_test"
    settings = {
        "render.color_distance": "rgb",
        "device.default_panel_profile": "gdep073e01_6c",
    }
    settings.update(extra_settings or {})
    monkeypatch.setitem(
        SYSTEM_PRESETS,
        key,
        {
            "key": key,
            "label_zh_tw": "Atomic test",
            "description": "Test-only preset registered by monkeypatch.",
            "settings": settings,
            "compatible_panel_profiles": ["safe_4c", "gdep073e01_6c"],
            "requires_device_confirmation": True,
            "renderer_version": "atomic-test-v1",
        },
    )
    return key


def _publish_safe_release(app, name: str) -> str:
    staged = app.extensions["inktime_release_publisher"].publish(
        [(name, Image.new("RGB", (480, 800), "white"))],
        profile_key="safe_4c",
        activate=False,
    )
    release = app.extensions["inktime_release_coordinator"].publish(
        [staged], created_by="settings-atomic-test", photo_ids=[]
    )[0]
    return str(release["release_id"])


def _device_mutation_state(app, device_ids: list[str]) -> dict[str, dict]:
    with app.extensions["inktime_database"].session() as connection:
        result = {}
        for device_id in device_ids:
            device = connection.execute(
                """SELECT panel_profile,config_version,offline_schedule_version,
                          next_offline_prepare_at,updated_at
                   FROM devices WHERE id=?""",
                (device_id,),
            ).fetchone()
            queues = connection.execute(
                """SELECT id,status,updated_at FROM device_content_queue_items
                   WHERE device_id=? ORDER BY id""",
                (device_id,),
            ).fetchall()
            result[device_id] = {
                "device": dict(device),
                "queues": [dict(row) for row in queues],
            }
    return result


def _audit_counts(app) -> tuple[int, int]:
    with app.extensions["inktime_database"].session() as connection:
        return (
            int(connection.execute("SELECT COUNT(*) FROM setting_history").fetchone()[0]),
            int(connection.execute("SELECT COUNT(*) FROM settings_snapshots").fetchone()[0]),
        )


def test_metadata_is_complete_and_uses_zh_tw_labels(client, app):
    create_admin(app)
    login(client)
    response = client.get("/api/v1/settings/metadata")
    assert response.status_code == 200
    assert response.json["schema_version"] == 1
    required = {
        "key",
        "label_zh_tw",
        "category",
        "description",
        "risk",
        "type",
        "default",
        "min",
        "max",
        "choices",
        "choice_labels",
        "safe_fallback",
        "visibility",
        "advanced",
        "secret",
        "restart_required",
        "effective_scope",
        "cache_impact",
        "reanalysis_impact",
        "rerender_impact",
        "device_override_allowed",
        "dependencies",
        "conflicts",
        "validation_group",
    }
    assert all(required <= set(item) for item in response.json["settings"])
    labels = {item["key"]: item["label_zh_tw"] for item in response.json["settings"]}
    assert labels["analysis.ai_daily_photo_limit"] == "每日 AI 分析照片上限"
    assert all(item["risk"] in {"low", "medium", "high"} for item in response.json["settings"])
    assert all(item["secret"] is False for item in response.json["settings"])


def test_partial_update_only_writes_changed_keys_and_creates_one_snapshot(client, app):
    create_admin(app)
    login(client)
    response = _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 2, "general.timezone": "Asia/Taipei"},
        confirm=True,
    )
    assert response.status_code == 200
    assert response.json["updated"] == 1
    assert response.json["changed_keys"] == ["analysis.concurrency"]
    assert response.json["snapshot_id"]
    with app.extensions["inktime_database"].session() as connection:
        history = connection.execute("SELECT key FROM setting_history").fetchall()
        snapshots = connection.execute("SELECT changed_keys_json FROM settings_snapshots").fetchall()
    assert [row["key"] for row in history] == ["analysis.concurrency"]
    assert json.loads(snapshots[0]["changed_keys_json"]) == ["analysis.concurrency"]


def test_offline_policy_change_invalidates_only_eligible_device_deadlines(client, app):
    create_admin(app)
    login(client)
    devices = app.extensions["inktime_device_repository"]
    eligible_12, _ = devices.create(
        "eligible-12",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    eligible_24, _ = devices.create(
        "eligible-24",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
        offline_schedule_max_slots=24,
    )
    disabled, _ = devices.create(
        "disabled",
        enabled=False,
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    online, _ = devices.create("online")
    ambiguous, _ = devices.create(
        "ambiguous",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    future = "2099-01-01T00:00:00+00:00"
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id IN (?,?,?,?,?)",
            (future, eligible_12, eligible_24, disabled, online, ambiguous),
        )
        connection.execute(
            """UPDATE devices
               SET offline_schedule_capability_state='legacy_ambiguous'
               WHERE id=?""",
            (ambiguous,),
        )

    response = _post(
        client,
        "/api/v1/settings",
        {"offline.server_prefetch_margin_minutes": 20},
    )

    assert response.status_code == 200
    assert response.json["runtime_effects"]["offline_prepare_deadlines_invalidated"] == 2
    with app.extensions["inktime_database"].session() as connection:
        rows = connection.execute(
            "SELECT id,next_offline_prepare_at FROM devices ORDER BY id"
        ).fetchall()
    deadlines = {str(row["id"]): row["next_offline_prepare_at"] for row in rows}
    assert deadlines[eligible_12] == OFFLINE_PREPARE_BOOTSTRAP_AT
    assert deadlines[eligible_24] == OFFLINE_PREPARE_BOOTSTRAP_AT
    assert deadlines[disabled] == future
    assert deadlines[online] == future
    assert deadlines[ambiguous] == future

    unchanged = _post(
        client,
        "/api/v1/settings",
        {"offline.server_prefetch_margin_minutes": 20},
    )
    assert unchanged.json["updated"] == 0
    assert unchanged.json["runtime_effects"] == {}

    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            (future, eligible_12),
        )
    unrelated = _post(
        client,
        "/api/v1/settings",
        {"backup.retention": 15},
    )
    assert unrelated.status_code == 200
    assert unrelated.json["runtime_effects"] == {}
    assert devices.get(eligible_12)["next_offline_prepare_at"] == future


@pytest.mark.parametrize(
    ("key", "values"),
    [
        ("offline.server_prefetch_margin_minutes", (60, 15)),
        ("offline.future_schedule_prepare_hour_local", (18, 20)),
    ],
)
def test_offline_policy_changes_in_both_directions_rebootstrap_deadline(
    app, key, values
):
    devices = app.extensions["inktime_device_repository"]
    service = app.extensions["inktime_settings_mutation_service"]
    device_id, _ = devices.create(
        f"policy-{key}",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    for value in values:
        with app.extensions["inktime_database"].transaction() as connection:
            connection.execute(
                "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
                ("2099-01-01T00:00:00+00:00", device_id),
            )
        result = service.update_many(
            {key: value},
            changed_by="test",
            source_ip="127.0.0.1",
        )
        assert result["runtime_effects"]["offline_prepare_deadlines_invalidated"] == 1
        assert devices.get(device_id)["next_offline_prepare_at"] == OFFLINE_PREPARE_BOOTSTRAP_AT


def test_settings_side_effect_failure_rolls_back_setting_and_audit(app, monkeypatch):
    service = app.extensions["inktime_settings_mutation_service"]

    def fail_effect(*, connection):
        del connection
        raise sqlite3.IntegrityError("effect rollback")

    monkeypatch.setattr(
        app.extensions["inktime_offline_schedule_repository"],
        "invalidate_prepare_deadlines_for_policy_change",
        fail_effect,
    )
    with pytest.raises(sqlite3.IntegrityError, match="effect rollback"):
        service.update_many(
            {"offline.future_schedule_prepare_hour_local": 21},
            changed_by="test",
            source_ip="127.0.0.1",
        )
    assert (
        app.extensions["inktime_settings_repository"].get(
            "offline.future_schedule_prepare_hour_local"
        )
        == 20
    )
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM setting_history").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM settings_snapshots").fetchone()[0] == 0


def test_import_and_rollback_share_offline_policy_invalidation(client, app):
    create_admin(app)
    login(client)
    device_id, _ = app.extensions["inktime_device_repository"].create(
        "policy-paths",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    changed = _post(
        client,
        "/api/v1/settings",
        {"offline.future_schedule_prepare_hour_local": 21},
    )
    snapshot_id = changed.json["snapshot_id"]

    def set_future() -> None:
        with app.extensions["inktime_database"].transaction() as connection:
            connection.execute(
                "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
                ("2099-01-01T00:00:00+00:00", device_id),
            )

    set_future()
    rolled_back = _post(
        client,
        f"/api/v1/settings/snapshots/{snapshot_id}/rollback",
        {"confirm": True},
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json["runtime_effects"]["offline_prepare_deadlines_invalidated"] == 1

    set_future()
    imported = _post(
        client,
        "/api/v1/settings/import",
        {
            "confirm": True,
            "document": {
                "format": "inktime-settings",
                "version": 1,
                "settings": {"offline.future_schedule_prepare_hour_local": 22},
            },
        },
    )
    assert imported.status_code == 200
    assert imported.json["runtime_effects"]["offline_prepare_deadlines_invalidated"] == 1
    assert app.extensions["inktime_device_repository"].get(device_id)[
        "next_offline_prepare_at"
    ] == OFFLINE_PREPARE_BOOTSTRAP_AT


def test_preset_apply_uses_atomic_application_coordinator(client, app, monkeypatch):
    create_admin(app)
    login(client)
    service = app.extensions["inktime_settings_mutation_service"]
    original = service.apply_preset_atomic
    calls: list[tuple[dict, list[str]]] = []

    def record(settings, **kwargs):
        calls.append((dict(settings), list(kwargs["device_ids"])))
        return original(settings, **kwargs)

    monkeypatch.setattr(service, "apply_preset_atomic", record)
    response = _post(
        client,
        "/api/v1/settings/presets/gooddisplay_spectra6/apply",
        {},
    )
    assert response.status_code == 200
    assert calls and "render.profile" in calls[0][0]
    assert calls[0][1] == []


def test_import_applies_offline_and_timezone_effects(client, app):
    create_admin(app)
    login(client)
    device_id, _ = app.extensions["inktime_device_repository"].create(
        "import-effects",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    future = "2099-01-01T00:00:00+00:00"
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            (future, device_id),
        )
    response = _post(
        client,
        "/api/v1/settings/import",
        {
            "confirm": True,
            "document": {
                "format": "inktime-settings",
                "version": 1,
                "settings": {
                    "offline.server_prefetch_margin_minutes": 60,
                    "general.timezone": "America/Los_Angeles",
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json["runtime_effects"]["offline_prepare_deadlines_invalidated"] == 1
    assert response.json["runtime_effects"]["scheduled_tasks_rebased"] == 5
    assert app.extensions["inktime_device_repository"].get(device_id)[
        "next_offline_prepare_at"
    ] == OFFLINE_PREPARE_BOOTSTRAP_AT
    assert app.extensions["inktime_settings_repository"].get("general.timezone") == (
        "America/Los_Angeles"
    )
    enabled = [
        task for task in app.extensions["inktime_schedule_repository"].list() if task["enabled"]
    ]
    assert all(
        datetime.fromisoformat(str(task["next_run"])).utcoffset()
        in {timedelta(hours=-7), timedelta(hours=-8)}
        for task in enabled
    )


def test_import_effect_failure_rolls_back_settings_audit_deadline_cursor_and_cache(
    client, app, monkeypatch
):
    create_admin(app)
    login(client)
    settings = app.extensions["inktime_settings_repository"]
    service = app.extensions["inktime_settings_mutation_service"]
    device_id, _ = app.extensions["inktime_device_repository"].create(
        "import-rollback",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    future = "2099-01-01T00:00:00+00:00"
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            (future, device_id),
        )
    before_tasks = {
        task["key"]: task["next_run"]
        for task in app.extensions["inktime_schedule_repository"].list()
    }
    before_audit = _audit_counts(app)
    assert settings.get("offline.server_prefetch_margin_minutes") == 15

    def fail_rebase(*args, **kwargs):
        del args, kwargs
        raise sqlite3.IntegrityError("timezone rebase rollback")

    monkeypatch.setattr(service.schedules, "rebase_enabled_next_runs", fail_rebase)
    with pytest.raises(sqlite3.IntegrityError, match="timezone rebase rollback"):
        _post(
            client,
            "/api/v1/settings/import",
            {
                "confirm": True,
                "document": {
                    "format": "inktime-settings",
                    "version": 1,
                    "settings": {
                        "offline.server_prefetch_margin_minutes": 60,
                        "general.timezone": "America/Los_Angeles",
                    },
                },
            },
        )
    assert settings.get("offline.server_prefetch_margin_minutes") == 15
    assert settings.get("general.timezone") == "Asia/Taipei"
    assert app.extensions["inktime_device_repository"].get(device_id)[
        "next_offline_prepare_at"
    ] == future
    assert {
        task["key"]: task["next_run"]
        for task in app.extensions["inktime_schedule_repository"].list()
    } == before_tasks
    assert _audit_counts(app) == before_audit


def test_snapshot_rollback_reads_and_applies_inside_one_effect_transaction(app, monkeypatch):
    settings = app.extensions["inktime_settings_repository"]
    service = app.extensions["inktime_settings_mutation_service"]
    device_id, _ = app.extensions["inktime_device_repository"].create(
        "rollback-atomic",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    source = service.update_many(
        {
            "offline.server_prefetch_margin_minutes": 60,
            "general.timezone": "America/Los_Angeles",
        },
        changed_by="test",
        source_ip="127.0.0.1",
    )["snapshot_id"]
    future = "2099-01-01T00:00:00+00:00"
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id=?",
            (future, device_id),
        )
    before_tasks = {
        task["key"]: task["next_run"]
        for task in app.extensions["inktime_schedule_repository"].list()
    }
    before_audit = _audit_counts(app)

    def fail_rebase(*args, **kwargs):
        del args, kwargs
        raise sqlite3.IntegrityError("rollback cursor failure")

    monkeypatch.setattr(service.schedules, "rebase_enabled_next_runs", fail_rebase)
    with pytest.raises(sqlite3.IntegrityError, match="rollback cursor failure"):
        service.rollback(
            str(source),
            changed_by="test",
            source_ip="127.0.0.1",
        )
    assert settings.get("offline.server_prefetch_margin_minutes") == 60
    assert settings.get("general.timezone") == "America/Los_Angeles"
    assert app.extensions["inktime_device_repository"].get(device_id)[
        "next_offline_prepare_at"
    ] == future
    assert {
        task["key"]: task["next_run"]
        for task in app.extensions["inktime_schedule_repository"].list()
    } == before_tasks
    assert _audit_counts(app) == before_audit


def test_preset_device_failure_rolls_back_every_selected_device_and_settings(app, monkeypatch):
    devices = app.extensions["inktime_device_repository"]
    service = app.extensions["inktime_settings_mutation_service"]
    device_ids = [devices.create(f"preset-device-{index}")[0] for index in range(3)]
    before = _device_mutation_state(app, device_ids)
    before_audit = _audit_counts(app)
    original = devices.update_render_inputs_in_transaction
    calls = 0

    def fail_second(connection, device_id, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.IntegrityError("second device rollback")
        return original(connection, device_id, **kwargs)

    monkeypatch.setattr(devices, "update_render_inputs_in_transaction", fail_second)
    with pytest.raises(sqlite3.IntegrityError, match="second device rollback"):
        service.apply_preset_atomic(
            {
                "render.color_distance": "rgb",
                "device.default_panel_profile": "gdep073e01_6c",
            },
            device_ids=device_ids,
            compatible_panel_profiles={"safe_4c"},
            target_panel_profile="gdep073e01_6c",
            changed_by="test",
            source_ip="127.0.0.1",
            reason="preset:fault-device",
        )
    assert _device_mutation_state(app, device_ids) == before
    assert app.extensions["inktime_settings_repository"].get("render.color_distance") == "oklab"
    assert _audit_counts(app) == before_audit


def test_preset_settings_failure_rolls_back_device_changes(app, monkeypatch):
    devices = app.extensions["inktime_device_repository"]
    service = app.extensions["inktime_settings_mutation_service"]
    device_ids = [devices.create(f"preset-settings-{index}")[0] for index in range(2)]
    before = _device_mutation_state(app, device_ids)
    before_audit = _audit_counts(app)

    def fail_settings(*args, **kwargs):
        del args, kwargs
        raise sqlite3.IntegrityError("settings rollback")

    monkeypatch.setattr(service, "update_many_in_transaction", fail_settings)
    with pytest.raises(sqlite3.IntegrityError, match="settings rollback"):
        service.apply_preset_atomic(
            {
                "render.color_distance": "rgb",
                "device.default_panel_profile": "gdep073e01_6c",
            },
            device_ids=device_ids,
            compatible_panel_profiles={"safe_4c"},
            target_panel_profile="gdep073e01_6c",
            changed_by="test",
            source_ip="127.0.0.1",
            reason="preset:fault-settings",
        )
    assert _device_mutation_state(app, device_ids) == before
    assert app.extensions["inktime_settings_repository"].get("render.color_distance") == "oklab"
    assert _audit_counts(app) == before_audit


def test_preset_effect_failure_rolls_back_devices_settings_history_snapshot_and_deadline(
    app, monkeypatch
):
    devices = app.extensions["inktime_device_repository"]
    service = app.extensions["inktime_settings_mutation_service"]
    selected, _ = devices.create(
        "preset-effect-selected",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    untouched, _ = devices.create(
        "preset-effect-untouched",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    release_id = _publish_safe_release(app, "preset-effect-release")
    target = (datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)).isoformat()
    app.extensions["inktime_offline_schedule_repository"].prepare_day(
        device_id=selected,
        target_date=target,
        release_ids=[release_id],
    )
    future = "2099-01-01T00:00:00+00:00"
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE devices SET next_offline_prepare_at=? WHERE id IN (?,?)",
            (future, selected, untouched),
        )
    before = _device_mutation_state(app, [selected, untouched])
    before_audit = _audit_counts(app)

    def fail_effect(*, connection):
        del connection
        raise sqlite3.IntegrityError("derived effect rollback")

    monkeypatch.setattr(
        service.offline_schedules,
        "invalidate_prepare_deadlines_for_policy_change",
        fail_effect,
    )
    with pytest.raises(sqlite3.IntegrityError, match="derived effect rollback"):
        service.apply_preset_atomic(
            {
                "render.color_distance": "rgb",
                "device.default_panel_profile": "gdep073e01_6c",
                "offline.server_prefetch_margin_minutes": 60,
            },
            device_ids=[selected],
            compatible_panel_profiles={"safe_4c"},
            target_panel_profile="gdep073e01_6c",
            changed_by="test",
            source_ip="127.0.0.1",
            reason="preset:fault-effect",
        )
    assert _device_mutation_state(app, [selected, untouched]) == before
    assert app.extensions["inktime_settings_repository"].get("render.color_distance") == "oklab"
    assert app.extensions["inktime_settings_repository"].get(
        "offline.server_prefetch_margin_minutes"
    ) == 15
    assert _audit_counts(app) == before_audit


def test_preset_success_is_atomic_bounded_and_idempotent(client, app, monkeypatch):
    create_admin(app)
    login(client)
    key = _install_atomic_test_preset(monkeypatch)
    devices = app.extensions["inktime_device_repository"]
    selected = [
        devices.create(
            f"preset-success-{index}",
            delivery_mode="inktime_offline_schedule",
            schedule_times=["08:00"],
        )[0]
        for index in range(2)
    ]
    unselected, _ = devices.create(
        "preset-unselected",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    release_id = _publish_safe_release(app, "preset-atomic-release")
    target = (datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)).isoformat()
    offline = app.extensions["inktime_offline_schedule_repository"]
    schedules = [
        offline.prepare_day(
            device_id=device_id,
            target_date=target,
            release_ids=[release_id],
        )["schedule"]["id"]
        for device_id in selected
    ]
    before = _device_mutation_state(app, selected + [unselected])
    before_audit = _audit_counts(app)
    response = _post(
        client,
        f"/api/v1/settings/presets/{key}/apply",
        {
            "update_existing_device_ids": selected,
            "confirm_physical_panel": True,
        },
    )
    assert response.status_code == 200
    assert response.json["changed_device_count"] == 2
    assert response.json["affected_devices"] == selected
    assert response.json["updated"] == 1
    after = _device_mutation_state(app, selected + [unselected])
    for device_id in selected:
        assert after[device_id]["device"]["panel_profile"] == "gdep073e01_6c"
        assert after[device_id]["device"]["config_version"] == (
            before[device_id]["device"]["config_version"] + 1
        )
        assert after[device_id]["device"]["offline_schedule_version"] == before[device_id][
            "device"
        ]["offline_schedule_version"]
        assert after[device_id]["device"]["next_offline_prepare_at"] == (
            OFFLINE_PREPARE_BOOTSTRAP_AT
        )
        assert {item["status"] for item in after[device_id]["queues"]} == {"CANCELLED"}
    assert after[unselected] == before[unselected]
    with app.extensions["inktime_database"].session() as connection:
        assert {
            connection.execute(
                "SELECT status FROM device_offline_schedules WHERE id=?", (schedule_id,)
            ).fetchone()["status"]
            for schedule_id in schedules
        } == {"ready"}
    assert app.extensions["inktime_settings_repository"].get("render.color_distance") == "rgb"
    assert _audit_counts(app) == (before_audit[0] + 1, before_audit[1] + 1)

    before_second = _device_mutation_state(app, selected + [unselected])
    before_second_audit = _audit_counts(app)
    repeated = _post(
        client,
        f"/api/v1/settings/presets/{key}/apply",
        {
            "update_existing_device_ids": selected,
            "confirm_physical_panel": True,
        },
    )
    assert repeated.status_code == 200
    assert repeated.json["changed_device_count"] == 0
    assert repeated.json["updated"] == 0
    assert _device_mutation_state(app, selected + [unselected]) == before_second
    assert _audit_counts(app) == before_second_audit


def test_normal_device_update_and_atomic_preset_have_render_state_parity(
    client, app, monkeypatch
):
    create_admin(app)
    login(client)
    key = _install_atomic_test_preset(monkeypatch)
    devices = app.extensions["inktime_device_repository"]
    api_device, _ = devices.create(
        "render-parity-api",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    preset_device, _ = devices.create(
        "render-parity-preset",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    response = client.patch(
        f"/api/v1/devices/{api_device}",
        json={"panel_profile": "gdep073e01_6c"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    preset = _post(
        client,
        f"/api/v1/settings/presets/{key}/apply",
        {
            "update_existing_device_ids": [preset_device],
            "confirm_physical_panel": True,
        },
    )
    assert preset.status_code == 200
    states = _device_mutation_state(app, [api_device, preset_device])
    comparable = (
        "panel_profile",
        "config_version",
        "offline_schedule_version",
        "next_offline_prepare_at",
    )
    assert {
        key: states[api_device]["device"][key] for key in comparable
    } == {key: states[preset_device]["device"][key] for key in comparable}


def test_runtime_timezone_change_rebases_enabled_cron_without_enqueuing(app):
    assert SETTING_DEFINITIONS["general.timezone"]["restart"] is False
    database = app.extensions["inktime_database"]
    service = app.extensions["inktime_settings_mutation_service"]
    fixed_now = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)

    def fixed_clock() -> datetime:
        return fixed_now

    service.clock = fixed_clock
    with database.transaction() as connection:
        connection.execute(
            """UPDATE scheduled_tasks
               SET cron='30 7 * * *',weekdays_json='[]',
                   last_success='enabled-success',last_failure='enabled-failure',
                   error_status='enabled-retry'
            """
            "WHERE key='incremental_scan'"
        )
        connection.execute(
            "UPDATE scheduled_tasks SET cron='0 4 1 * *',weekdays_json='[]' "
            "WHERE key='full_reconcile'"
        )
        connection.execute(
            "UPDATE scheduled_tasks SET cron='0 1 1 1 *',weekdays_json='[]' "
            "WHERE key='ai_schedule'"
        )
        connection.execute(
            "UPDATE scheduled_tasks SET cron='0 9 * * *',weekdays_json='[0,2]' "
            "WHERE key='display_prepare'"
        )
        connection.execute(
            """UPDATE scheduled_tasks
               SET enabled=0,next_run=NULL,last_success='success-marker',
                   last_failure='failure-marker',error_status='retry-marker'
               WHERE key='cache_cleanup'"""
        )
    before_jobs = len(app.extensions["inktime_job_repository"].list())

    result = service.update_many(
        {"general.timezone": "America/Los_Angeles"},
        changed_by="test",
        source_ip="127.0.0.1",
    )

    assert result["runtime_effects"]["scheduled_tasks_rebased"] == 4
    tasks = {
        task["key"]: task for task in app.extensions["inktime_schedule_repository"].list()
    }
    local_runs = {
        key: datetime.fromisoformat(str(tasks[key]["next_run"])).astimezone(
            ZoneInfo("America/Los_Angeles")
        )
        for key in ("incremental_scan", "full_reconcile", "ai_schedule", "display_prepare")
    }
    assert (local_runs["incremental_scan"].month, local_runs["incremental_scan"].day) == (3, 7)
    assert (local_runs["incremental_scan"].hour, local_runs["incremental_scan"].minute) == (7, 30)
    assert (
        tasks["incremental_scan"]["last_success"],
        tasks["incremental_scan"]["last_failure"],
        tasks["incremental_scan"]["error_status"],
    ) == ("enabled-success", "enabled-failure", "enabled-retry")
    assert (local_runs["full_reconcile"].month, local_runs["full_reconcile"].day) == (4, 1)
    assert (local_runs["ai_schedule"].year, local_runs["ai_schedule"].month) == (2027, 1)
    assert local_runs["display_prepare"].weekday() == 0
    assert local_runs["display_prepare"].hour == 9
    disabled = tasks["cache_cleanup"]
    assert disabled["next_run"] is None
    assert (
        disabled["last_success"],
        disabled["last_failure"],
        disabled["error_status"],
    ) == ("success-marker", "failure-marker", "retry-marker")
    assert len(app.extensions["inktime_job_repository"].list()) == before_jobs

    reversed_result = service.update_many(
        {"general.timezone": "Asia/Taipei"},
        changed_by="test",
        source_ip="127.0.0.1",
    )
    assert reversed_result["runtime_effects"]["scheduled_tasks_rebased"] == 4
    taipei_run = datetime.fromisoformat(
        str(app.extensions["inktime_schedule_repository"].get("incremental_scan")["next_run"])
    ).astimezone(ZoneInfo("Asia/Taipei"))
    assert (taipei_run.hour, taipei_run.minute) == (7, 30)
    assert len(app.extensions["inktime_job_repository"].list()) == before_jobs


def test_schedule_due_comparison_and_dst_are_absolute_and_nonduplicating(app):
    schedules = app.extensions["inktime_schedule_repository"]
    zone = ZoneInfo("America/Los_Angeles")
    spring = schedules._next_run("30 2 * * *", datetime(2026, 3, 8, 1, 59, tzinfo=zone), [])
    assert spring == datetime(2026, 3, 9, 2, 30, tzinfo=zone)
    first_fall = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    fall = schedules._next_run("30 1 * * *", first_fall, [])
    assert fall == datetime(2026, 11, 2, 1, 30, tzinfo=zone)

    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET enabled=1,next_run=? WHERE key='cache_cleanup'",
            ("2026-01-01T00:30:00+14:00",),
        )
    due = schedules.due(datetime(2025, 12, 31, 11, 0, tzinfo=timezone.utc))
    assert "cache_cleanup" in {task["key"] for task in due}


def test_unknown_key_rejects_entire_partial_update(client, app):
    create_admin(app)
    login(client)
    response = _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 2, "danger.shell": "rm"},
    )
    assert response.status_code == 400
    repository = app.extensions["inktime_settings_repository"]
    assert repository.get("analysis.concurrency") == 1
    assert repository.snapshots() == []


def test_cross_field_validation_uses_current_plus_partial_update(client, app):
    create_admin(app)
    login(client)
    invalid = _post(
        client,
        "/api/v1/settings/preview",
        {"analysis.caption_min_chars": 221},
    )
    assert invalid.status_code == 200
    assert invalid.json["valid"] is False
    assert "min ≤ target ≤ max" in invalid.json["validation_errors"][0]
    assert app.extensions["inktime_settings_repository"].get("analysis.caption_min_chars") == 120

    valid = client.post(
        "/api/v1/settings",
        json={
            "analysis.caption_min_chars": 180,
            "analysis.caption_target_chars": 200,
            "analysis.caption_max_chars": 240,
        },
        headers={
            "X-CSRF-Token": csrf(client),
            "X-InkTime-Confirm-Risk": "true",
        },
    )
    assert valid.status_code == 200
    assert valid.json["updated"] == 3


def test_high_risk_change_requires_preview_confirmation(client, app):
    create_admin(app)
    login(client)
    blocked = _post(
        client,
        "/api/v1/settings",
        {"analysis.ai_mode": "full_library"},
    )
    assert blocked.status_code == 409
    assert app.extensions["inktime_settings_repository"].get("analysis.ai_mode") == "top_candidates"
    confirmed = client.post(
        "/api/v1/settings",
        json={"analysis.ai_mode": "full_library"},
        headers={
            "X-CSRF-Token": csrf(client),
            "X-InkTime-Confirm-Risk": "true",
        },
    )
    assert confirmed.status_code == 200


def test_transaction_failure_leaves_no_setting_snapshot_or_history(app):
    repository = app.extensions["inktime_settings_repository"]
    database = app.extensions["inktime_database"]
    with database.session() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_timezone_update
            BEFORE UPDATE ON settings
            WHEN NEW.key='general.timezone' AND NEW.value_json='"UTC"'
            BEGIN SELECT RAISE(ABORT, 'test rollback'); END
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        repository.update_many(
            {"analysis.concurrency": 2, "general.timezone": "UTC"},
            changed_by="test",
            source_ip="127.0.0.1",
        )
    assert repository.get("analysis.concurrency") == 1
    assert repository.get("general.timezone") == "Asia/Taipei"
    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM settings_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM setting_history").fetchone()[0] == 0


def test_private_locations_are_redacted_from_snapshot_and_export(client, app):
    create_admin(app)
    login(client)
    response = _post(
        client,
        "/api/v1/settings",
        {
            "home_latitude": 24.987654,
            "render.font_path": "/Users/example/private-font.ttf",
        },
        confirm=True,
    )
    snapshot = app.extensions["inktime_settings_repository"].snapshot(response.json["snapshot_id"])
    assert "home_latitude" not in snapshot["before"]
    assert "home_latitude" not in snapshot["after"]
    assert all(item["old_value"] == {"status": "已設定"} for item in snapshot["items"])
    assert all(item["new_value"] == {"status": "已變更"} for item in snapshot["items"])

    exported = client.get("/api/v1/settings/export")
    assert exported.status_code == 200
    document = json.loads(exported.get_data(as_text=True))
    assert "home_latitude" not in document["settings"]
    assert document["sensitive_status"]["home_latitude"] == {"configured": True}
    assert "render.font_path" not in document["settings"]
    assert "/Users/example/private-font.ttf" not in exported.get_data(as_text=True)
    assert "webhook.bearer_token" not in exported.get_data(as_text=True)
    assert exported.headers["Cache-Control"] == "no-store"


def test_viewer_html_and_snapshot_apis_never_receive_sensitive_values(client, app):
    create_admin(app)
    login(client)
    exact_latitude = 24.987654
    exact_font_path = "/Users/example/private-font-unique.ttf"
    exact_webhook = "https://hooks.example.test/private/path?token=unique-token"
    changed = _post(
        client,
        "/api/v1/settings",
        {
            "home_latitude": exact_latitude,
            "render.font_path": exact_font_path,
            "notification.webhook_url": exact_webhook,
        },
        confirm=True,
    )
    assert changed.status_code == 200
    snapshot_id = changed.json["snapshot_id"]

    app.extensions["inktime_auth_repository"].create_user(
        "viewer-sensitive", "viewer-password-long", "viewer"
    )
    login(client, "viewer-sensitive", "viewer-password-long")
    settings_html = client.get("/settings").get_data(as_text=True)
    snapshot_body = client.get(f"/api/v1/settings/snapshots/{snapshot_id}").get_data(as_text=True)
    snapshot_list = client.get("/api/v1/settings/snapshots").get_data(as_text=True)
    metadata = client.get("/api/v1/settings/metadata").get_data(as_text=True)

    for secret in (
        str(exact_latitude),
        exact_font_path,
        exact_webhook,
        "unique-token",
        "127.0.0.1",
    ):
        assert secret not in settings_html
        assert secret not in snapshot_body
        assert secret not in snapshot_list
    assert str(exact_latitude) not in metadata
    assert exact_font_path not in metadata
    assert "source_ip" not in snapshot_body
    assert "source_ip" not in snapshot_list
    assert "已設定" in settings_html


def test_webhook_url_path_query_and_token_never_enter_snapshot_or_export(client, app):
    create_admin(app)
    login(client)
    webhook = "https://hooks.example.test/private/path?token=do-not-store"
    changed = _post(
        client,
        "/api/v1/settings",
        {"notification.webhook_url": webhook},
        confirm=True,
    )
    assert changed.status_code == 200
    snapshot_id = changed.json["snapshot_id"]
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            """
            SELECT before_json,after_json FROM settings_snapshots WHERE id=?
            """,
            (snapshot_id,),
        ).fetchone()
        item = connection.execute(
            """
            SELECT old_value_json,new_value_json
            FROM settings_snapshot_items WHERE snapshot_id=? AND key=?
            """,
            (snapshot_id, "notification.webhook_url"),
        ).fetchone()
    persisted_snapshot = " ".join(
        (row["before_json"], row["after_json"], item["old_value_json"], item["new_value_json"])
    )
    assert "/private/path" not in persisted_snapshot
    assert "do-not-store" not in persisted_snapshot
    exported = client.get("/api/v1/settings/export")
    assert exported.headers["Cache-Control"] == "no-store"
    assert "/private/path" not in exported.get_data(as_text=True)
    assert "do-not-store" not in exported.get_data(as_text=True)
    assert json.loads(exported.get_data(as_text=True))["sensitive_status"]["notification.webhook_url"] == {
        "configured": True
    }


def test_rollback_preview_and_apply_create_new_snapshot(client, app):
    create_admin(app)
    login(client)
    changed = _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 2},
        confirm=True,
    )
    source_snapshot = changed.json["snapshot_id"]
    preview = _post(
        client,
        f"/api/v1/settings/snapshots/{source_snapshot}/rollback-preview",
        {},
    )
    assert preview.status_code == 200
    assert preview.json["updates"]["analysis.concurrency"] == 1
    applied = _post(
        client,
        f"/api/v1/settings/snapshots/{source_snapshot}/rollback",
        {"confirm": True},
    )
    assert applied.status_code == 200
    assert app.extensions["inktime_settings_repository"].get("analysis.concurrency") == 1
    source = next(
        row
        for row in app.extensions["inktime_settings_repository"].all()
        if row["key"] == "analysis.concurrency"
    )
    assert source["effective_source"] == "Default"
    assert source["stored_value"] is None
    snapshots = app.extensions["inktime_settings_repository"].snapshots()
    assert len(snapshots) == 2
    assert snapshots[0]["rollback_source_snapshot_id"] == source_snapshot


def test_rollback_preview_is_exact_changed_keys_diff_and_marks_later_overwrite(client, app):
    create_admin(app)
    login(client)
    source = _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 2},
        confirm=True,
    ).json["snapshot_id"]
    assert (
        _post(
            client,
            "/api/v1/settings",
            {"general.timezone": "UTC"},
        ).status_code
        == 200
    )
    assert (
        _post(
            client,
            "/api/v1/settings",
            {"analysis.concurrency": 3},
            confirm=True,
        ).status_code
        == 200
    )

    preview = _post(
        client,
        f"/api/v1/settings/snapshots/{source}/rollback-preview",
        {},
    )
    assert preview.status_code == 200
    assert preview.json["rollback_scope"] == "snapshot_changed_keys_only"
    assert preview.json["updates"] == {"analysis.concurrency": 1}
    assert preview.json["diff"] == [
        {
            "key": "analysis.concurrency",
            "label_zh_tw": "AI 分析並行數",
            "current_value": 3,
            "target_value": 1,
            "changed_since_snapshot": True,
        }
    ]
    assert preview.json["overwrites_changes_after_snapshot"] is True
    assert "general.timezone" not in preview.json["updates"]


def test_removed_legacy_snapshot_key_is_serialized_and_skipped_safely(client, app):
    create_admin(app)
    login(client)
    snapshot_id = "legacy-removed-setting"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO settings_snapshots(
                id,created_at,actor_id,source_ip,reason,before_json,after_json,
                changed_keys_json,schema_version,application_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                "2026-07-24T00:00:00+00:00",
                "legacy-admin",
                "192.0.2.55",
                "legacy",
                '{"removed.private_key":"old-value"}',
                '{"removed.private_key":"new-value"}',
                '["removed.private_key"]',
                0,
                "legacy",
            ),
        )
        connection.execute(
            """
            INSERT INTO settings_snapshot_items(
                snapshot_id,key,old_value_json,new_value_json,restored_default
            ) VALUES (?,?,?,?,0)
            """,
            (
                snapshot_id,
                "removed.private_key",
                '"old-value"',
                '"new-value"',
            ),
        )

    detail = client.get(f"/api/v1/settings/snapshots/{snapshot_id}")
    assert detail.status_code == 200
    assert detail.json["items"][0]["metadata"]["label_zh_tw"] == "已移除設定"
    assert detail.json["items"][0]["metadata"]["removed"] is True
    assert detail.json["items"][0]["old_value"] == {"status": "已移除設定"}
    assert "old-value" not in detail.get_data(as_text=True)

    preview = _post(
        client,
        f"/api/v1/settings/snapshots/{snapshot_id}/rollback-preview",
        {},
    )
    assert preview.status_code == 200
    assert preview.json["unknown_keys"] == ["removed.private_key"]
    assert preview.json["updates"] == {}
    applied = _post(
        client,
        f"/api/v1/settings/snapshots/{snapshot_id}/rollback",
        {"confirm": True},
    )
    assert applied.status_code == 200
    assert applied.json["updated"] == 0


def test_sensitive_snapshot_keys_require_manual_rollback(client, app):
    create_admin(app)
    login(client)
    source = _post(
        client,
        "/api/v1/settings",
        {
            "home_latitude": 23.456789,
            "notification.webhook_url": "https://example.test/hook?token=manual",
        },
        confirm=True,
    ).json["snapshot_id"]
    preview = _post(
        client,
        f"/api/v1/settings/snapshots/{source}/rollback-preview",
        {},
    )
    assert preview.status_code == 200
    assert preview.json["sensitive_unrestorable_keys"] == [
        "home_latitude",
        "notification.webhook_url",
    ]
    assert preview.json["updates"] == {}
    assert preview.json["changed_keys"] == []


def test_import_preview_has_no_side_effect_and_apply_skips_unknown_keys(client, app):
    create_admin(app)
    login(client)
    document = {
        "format": "inktime-settings",
        "version": 1,
        "settings": {
            "analysis.concurrency": 3,
            "home_latitude": 20.0,
            "future.unknown": True,
            "docker.port": 9999,
        },
    }
    preview = _post(client, "/api/v1/settings/import-preview", document)
    assert preview.status_code == 200
    assert preview.json["changes"] == {"analysis.concurrency": 3}
    assert preview.json["unknown_keys"] == ["future.unknown"]
    assert preview.json["blocked_keys"] == ["docker.port", "home_latitude"]
    assert "部署" in preview.json["blocked_reasons"]["docker.port"]
    assert app.extensions["inktime_settings_repository"].get("analysis.concurrency") == 1
    assert app.extensions["inktime_settings_repository"].snapshots() == []

    applied = _post(
        client,
        "/api/v1/settings/import",
        {"confirm": True, "document": document},
    )
    assert applied.status_code == 200
    assert app.extensions["inktime_settings_repository"].get("analysis.concurrency") == 3


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(client, app, invalid):
    create_admin(app)
    login(client)
    response = _post(
        client,
        "/api/v1/settings",
        {"analysis.stage_two_threshold": invalid},
        confirm=True,
    )
    assert response.status_code == 400
    assert "有限數字" in response.json["message"]


def test_fractional_integer_is_rejected_without_truncation(client, app):
    create_admin(app)
    login(client)
    response = _post(
        client,
        "/api/v1/settings",
        {"analysis.concurrency": 1.9},
        confirm=True,
    )
    assert response.status_code == 400
    assert app.extensions["inktime_settings_repository"].get("analysis.concurrency") == 1


def test_runtime_unwired_setting_is_read_only_for_api_import_and_ui(client, app):
    create_admin(app)
    login(client)
    direct = _post(
        client,
        "/api/v1/settings",
        {"observability.debug_level": "detailed"},
        confirm=True,
    )
    assert direct.status_code == 400
    assert "僅供唯讀" in direct.json["message"]

    document = {
        "format": "inktime-settings",
        "version": 1,
        "settings": {"observability.debug_level": "detailed"},
    }
    preview = _post(client, "/api/v1/settings/import-preview", document)
    assert preview.status_code == 200
    assert preview.json["changes"] == {}
    assert preview.json["blocked_keys"] == ["observability.debug_level"]
    assert "尚未接上 Runtime" in preview.json["blocked_reasons"]["observability.debug_level"]

    body = client.get("/settings").get_data(as_text=True)
    assert 'data-key="observability.debug_level"' in body
    assert 'data-scope="not_wired"' in body
    assert "已儲存但尚未生效／尚未支援" in body
    assert 'name="observability.debug_level" disabled' in body


def test_device_override_and_effective_scope_metadata_use_actual_whitelists():
    allowed = {
        key for key, definition in SETTING_DEFINITIONS.items() if definition["device_override_allowed"]
    }
    assert allowed == DEVICE_OVERRIDE_KEYS
    assert SETTING_DEFINITIONS["render.layout"]["device_override_allowed"] is True
    assert SETTING_DEFINITIONS["render.dither"]["device_override_allowed"] is False
    assert SETTING_DEFINITIONS["render.layout"]["effective_scope"] == "next_render"
    assert SETTING_DEFINITIONS["device.default_schedule"]["effective_scope"] == "future_device_only"
    assert SETTING_DEFINITIONS["observability.debug_level"]["effective_scope"] == "not_wired"
    assert SETTING_DEFINITIONS["render.layout"]["existing_release_unchanged"] is True
    assert "建立新 Release" in SETTING_DEFINITIONS["render.layout"]["effective_note"]


def test_snapshot_retention_is_bounded_and_keeps_latest_rollback_source(app):
    repository = app.extensions["inktime_settings_repository"]
    first = repository.update_many(
        {"analysis.concurrency": 2},
        changed_by="test",
        source_ip="127.0.0.1",
    )["snapshot_id"]
    repository.rollback(
        first,
        changed_by="test",
        source_ip="127.0.0.1",
    )
    for index in range(105):
        repository.update_many(
            {"analysis.concurrency": 2 if index % 2 == 0 else 1},
            changed_by="test",
            source_ip="127.0.0.1",
        )
    snapshots = repository.snapshots(200)
    assert len(snapshots) == 100
    with app.extensions["inktime_database"].session() as connection:
        latest_rollback = connection.execute(
            """
            SELECT rollback_source_snapshot_id FROM settings_snapshots
            WHERE rollback_source_snapshot_id IS NOT NULL
            ORDER BY created_at DESC,id DESC LIMIT 1
            """
        ).fetchone()
        assert latest_rollback["rollback_source_snapshot_id"] == first
        assert (
            connection.execute("SELECT COUNT(*) FROM settings_snapshots WHERE id=?", (first,)).fetchone()[0]
            == 1
        )


def test_viewer_has_read_only_governed_ui(client, app):
    create_admin(app)
    app.extensions["inktime_auth_repository"].create_user("viewer", "viewer-password-long", "viewer")
    login(client, "viewer", "viewer-password-long")
    body = client.get("/settings").get_data(as_text=True)
    assert "設定控制中心" in body
    assert "每日 AI 分析照片上限" in body
    assert 'data-can-edit="false"' in body
    assert 'id="save-settings"' not in body
    assert "匯出安全設定" not in body
    assert _post(client, "/api/v1/settings", {"analysis.concurrency": 2}).status_code == 403


def test_ui_contains_dirty_search_filter_snapshot_and_accessibility_contracts(client, app):
    create_admin(app)
    login(client)
    body = client.get("/settings").get_data(as_text=True)
    for marker in (
        'id="settings-search"',
        'id="settings-category-filter"',
        'name="settings-mode"',
        'id="dirty-count"',
        "beforeunload",
        "Object.fromEntries(dirty)",
        'id="settings-preview-dialog"',
        'id="snapshot-dialog"',
        'id="import-dialog"',
        'role="alert"',
        "Rollback 實際 Diff",
        "敏感設定無法自動 Rollback",
        "只回復該 Snapshot 的 changed_keys",
        "既有 Release 不會改變",
    ):
        assert marker in body
    assert "完整裝置群組覆寫" not in body
    assert "改變 Cache Fingerprint" in body
    assert SETTING_DEFINITIONS["analysis.ai_daily_photo_limit"]["advanced"] is True
    assert SETTING_DEFINITIONS["analysis.caption_variants_enabled"]["advanced"] is True
    assert SETTING_DEFINITIONS["analysis.ai_daily_photo_limit"]["risk"] == "high"
    assert SETTING_DEFINITIONS["analysis.caption_variants_enabled"]["dependencies"] == [
        {"key": "analysis.advanced_caption_enabled", "equals": True}
    ]
