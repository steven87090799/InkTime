from __future__ import annotations

import pytest
from werkzeug.exceptions import BadRequest

from inktime.app.api.devices import _validated_device_fields


def _defaults(mode: str = "legacy_online") -> dict:
    return {
        "name": "契約測試裝置",
        "enabled": True,
        "timezone": "Asia/Taipei",
        "schedule": "08:00",
        "rotation": 0,
        "panel_profile": "safe_4c",
        "delivery_mode": mode,
        "schedule_times": ["08:00"],
        "prefetch_lead_minutes": 5,
        "button_wake_action": "check_new",
        "stock_endpoint_host": None,
        "frame_orientation": None,
        "layout_mode": None,
        "fit_mode": None,
    }


def test_omitted_prefetch_is_normalized_from_delivery_mode():
    enhanced = _validated_device_fields(
        {"delivery_mode": "inktime_offline_schedule"}, defaults=_defaults()
    )
    legacy = _validated_device_fields(
        {"delivery_mode": "legacy_online"}, defaults=_defaults("inktime_offline_schedule")
    )
    stock = _validated_device_fields(
        {"delivery_mode": "stock_compat"}, defaults=_defaults("inktime_offline_schedule")
    )
    assert enhanced["offline_prefetch_allowed"] is True
    assert legacy["offline_prefetch_allowed"] is False
    assert stock["offline_prefetch_allowed"] is False


@pytest.mark.parametrize(
    "delivery_mode",
    ["inktime_offline_schedule", "legacy_online", "stock_compat"],
)
def test_explicit_contradictory_prefetch_is_rejected(delivery_mode):
    contradictory = not delivery_mode == "inktime_offline_schedule"
    with pytest.raises(BadRequest, match="DEVICE-008"):
        _validated_device_fields(
            {
                "delivery_mode": delivery_mode,
                "offline_prefetch_allowed": contradictory,
            },
            defaults=_defaults(),
        )


def test_patch_mode_transition_without_prefetch_field_is_safe():
    enhanced = _validated_device_fields(
        {"delivery_mode": "inktime_offline_schedule"}, defaults=_defaults("legacy_online")
    )
    legacy = _validated_device_fields(
        {"delivery_mode": "legacy_online"}, defaults=_defaults("inktime_offline_schedule")
    )
    assert enhanced["offline_prefetch_allowed"] is True
    assert legacy["offline_prefetch_allowed"] is False


def test_repository_create_normalizes_omitted_value_and_rejects_bypass(app):
    repository = app.extensions["inktime_device_repository"]
    enhanced_id, _token = repository.create(
        "Repository Enhanced",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
    )
    assert repository.get(enhanced_id)["offline_prefetch_allowed"] == 1
    with pytest.raises(ValueError, match="DEVICE-008"):
        repository.create(
            "Contradictory Enhanced",
            delivery_mode="inktime_offline_schedule",
            offline_prefetch_allowed=False,
            schedule_times=["08:00"],
        )
    with pytest.raises(ValueError, match="DEVICE-008"):
        repository.create(
            "Contradictory Legacy",
            delivery_mode="legacy_online",
            offline_prefetch_allowed=True,
            schedule_times=["08:00"],
        )


def test_repository_uses_persisted_offline_slot_capability(app):
    repository = app.extensions["inktime_device_repository"]
    schedule_times = [f"{hour:02d}:00" for hour in range(13)]
    with pytest.raises(ValueError, match="1 到 12"):
        repository.create(
            "Legacy 13 Slot Device",
            delivery_mode="inktime_offline_schedule",
            schedule_times=schedule_times,
        )
    device_id, _token = repository.create(
        "New 24 Slot Device",
        delivery_mode="inktime_offline_schedule",
        schedule_times=schedule_times,
        offline_schedule_max_slots=24,
    )
    assert repository.get(device_id)["offline_schedule_max_slots"] == 24


def test_repository_preserves_malformed_quarantine_and_avoids_idempotent_version_churn(app):
    repository = app.extensions["inktime_device_repository"]
    device_id, _token = repository.create(
        "Malformed repository quarantine",
        delivery_mode="inktime_offline_schedule",
        schedule_times=["08:00"],
        offline_schedule_max_slots=24,
    )
    schedule_times_raw = '["08:00",'
    offline_schedule_raw = '{"legacy":"08:00"}'
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            UPDATE devices
            SET offline_schedule_max_slots=12,
                offline_schedule_capability_state='legacy_ambiguous',
                schedule_times_json=?,offline_schedule_json=?,
                next_offline_prepare_at=NULL
            WHERE id=?
            """,
            (schedule_times_raw, offline_schedule_raw, device_id),
        )

    def disable() -> None:
        current = repository.get(device_id)
        repository.update(
            device_id,
            name=str(current["name"]),
            enabled=False,
            timezone_name=str(current["timezone"]),
            schedule=str(current["schedule"]),
            delivery_mode="inktime_offline_schedule",
            offline_prefetch_allowed=True,
            rotation=int(current["rotation"]),
            panel_profile=str(current["panel_profile"]),
        )

    before = repository.get(device_id)
    disable()
    after_disable = repository.get(device_id)
    assert after_disable["offline_schedule_version"] == before["offline_schedule_version"]
    versions = (
        int(after_disable["config_version"]),
        int(after_disable["offline_schedule_version"]),
    )
    disable()
    after_repeated = repository.get(device_id)

    assert after_repeated["schedule_times_json"] == schedule_times_raw
    assert after_repeated["offline_schedule_json"] == offline_schedule_raw
    assert after_repeated["offline_schedule_capability_state"] == "legacy_ambiguous"
    assert (
        int(after_repeated["config_version"]),
        int(after_repeated["offline_schedule_version"]),
    ) == versions
