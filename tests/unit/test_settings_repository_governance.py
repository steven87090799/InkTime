from __future__ import annotations

import math

import pytest

from inktime.app.repositories.settings import (
    DEVICE_OVERRIDE_KEYS,
    SETTING_DEFINITIONS,
    SettingsRepository,
)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_number_coercion_rejects_non_finite_values(value):
    with pytest.raises(ValueError, match="有限數字"):
        SettingsRepository._coerce("analysis.stage_two_threshold", value)


@pytest.mark.parametrize("value", [1.9, "1.9", math.nan, math.inf])
def test_integer_coercion_never_truncates_fractional_or_non_finite_values(value):
    with pytest.raises(ValueError, match="必須是整數"):
        SettingsRepository._coerce("analysis.concurrency", value)


def test_effective_scopes_and_device_overrides_are_explicit():
    assert {
        key
        for key, definition in SETTING_DEFINITIONS.items()
        if definition["device_override_allowed"]
    } == DEVICE_OVERRIDE_KEYS
    assert SETTING_DEFINITIONS["render.layout"]["effective_scope"] == "next_render"
    assert (
        SETTING_DEFINITIONS["device.default_schedule"]["effective_scope"]
        == "future_device_only"
    )
    assert (
        SETTING_DEFINITIONS["observability.debug_level"]["effective_scope"]
        == "not_wired"
    )
    assert SETTING_DEFINITIONS["render.dither"]["device_override_allowed"] is False


def test_removed_snapshot_metadata_is_safe_and_non_actionable():
    metadata = SettingsRepository.public_metadata("removed.private_key")
    assert metadata["label_zh_tw"] == "已移除設定"
    assert metadata["removed"] is True
    assert metadata["runtime_wired"] is False
    assert metadata["device_override_allowed"] is False
