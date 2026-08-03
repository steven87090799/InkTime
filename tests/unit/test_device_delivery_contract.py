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
