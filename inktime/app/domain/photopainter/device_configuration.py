"""Canonical device configuration change classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEVICE_RENDER_INPUT_FIELDS = frozenset(
    {"panel_profile", "rotation", "frame_orientation", "layout_mode", "fit_mode"}
)
DEVICE_OFFLINE_SCHEDULE_FIELDS = frozenset(
    {
        "timezone",
        "schedule_definition",
        "delivery_mode",
        "offline_prefetch_allowed",
        "prefetch_lead_minutes",
        "button_wake_action",
        "minimum_schedule_gap_minutes",
        "sync_strategy",
        "sync_time",
    }
)
DEVICE_REMOTE_CONFIG_FIELDS = frozenset(
    {
        *DEVICE_RENDER_INPUT_FIELDS,
        *DEVICE_OFFLINE_SCHEDULE_FIELDS,
        "enabled",
        "stock_endpoint_host",
    }
)


@dataclass(frozen=True)
class DeviceConfigurationChanges:
    changed_fields: frozenset[str]

    @property
    def render_inputs_changed(self) -> bool:
        return bool(self.changed_fields & DEVICE_RENDER_INPUT_FIELDS)

    @property
    def offline_schedule_changed(self) -> bool:
        return bool(self.changed_fields & DEVICE_OFFLINE_SCHEDULE_FIELDS)

    @property
    def remote_config_changed(self) -> bool:
        return bool(self.changed_fields & DEVICE_REMOTE_CONFIG_FIELDS)


def classify_device_configuration_changes(
    current: Mapping[str, Any],
    updated: Mapping[str, Any],
    *,
    schedule_definition_changed: bool = False,
) -> DeviceConfigurationChanges:
    """Classify persisted inputs without coupling rendering to schedule versions."""

    changed = {
        field
        for field in DEVICE_REMOTE_CONFIG_FIELDS - {"schedule_definition"}
        if current.get(field) != updated.get(field)
    }
    if schedule_definition_changed:
        changed.add("schedule_definition")
    return DeviceConfigurationChanges(frozenset(changed))
