from __future__ import annotations

import math

import pytest

from inktime.app.repositories.settings import (
    DEVICE_OVERRIDE_KEYS,
    SETTING_DEFINITIONS,
    SettingsRepository,
)


def test_project_photo_defaults_use_automatic_landscape_cover():
    assert SETTING_DEFINITIONS["render.layout"]["default"] == "auto_photo"
    assert SETTING_DEFINITIONS["render.frame_orientation"]["default"] == "landscape"
    assert SETTING_DEFINITIONS["render.fit_mode"]["default"] == "cover"


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
        key for key, definition in SETTING_DEFINITIONS.items() if definition["device_override_allowed"]
    } == DEVICE_OVERRIDE_KEYS
    assert SETTING_DEFINITIONS["render.layout"]["effective_scope"] == "next_render"
    assert SETTING_DEFINITIONS["device.default_schedule"]["effective_scope"] == "future_device_only"
    assert SETTING_DEFINITIONS["observability.debug_level"]["effective_scope"] == "not_wired"
    assert SETTING_DEFINITIONS["render.dither"]["device_override_allowed"] is False


def test_removed_snapshot_metadata_is_safe_and_non_actionable():
    metadata = SettingsRepository.public_metadata("removed.private_key")
    assert metadata["label_zh_tw"] == "已移除設定"
    assert metadata["removed"] is True
    assert metadata["runtime_wired"] is False
    assert metadata["device_override_allowed"] is False


@pytest.mark.parametrize("key", ["model.analysis_model", "budget.daily_stop", "analysis.concurrency"])
def test_paid_analysis_controls_remain_available_in_manual_and_seasonal_modes(key):
    rule = SettingsRepository.public_metadata(key)["dependencies"][0]
    assert rule["key"] == "analysis.execution_mode"
    assert rule["one_of"] == ["local_with_manual_ai", "automatic_ai", "seasonal_ai"]


@pytest.mark.parametrize("action", ["refresh", "invalidate"])
def test_cache_reader_remains_consistent_during_refresh_or_invalidation(app, action):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    repository = app.extensions["inktime_settings_repository"]
    key = "analysis.concurrency"
    expected = repository.get(key)
    reader_checked = Event()
    continue_reader = Event()
    writer_started = Event()

    class PausedCache(dict):
        def __contains__(self, lookup):
            found = super().__contains__(lookup)
            reader_checked.set()
            assert continue_reader.wait(5)
            return found

    repository._runtime_cache = PausedCache(repository._runtime_cache)

    def write():
        writer_started.set()
        if action == "refresh":
            return repository.get("general.timezone")
        repository.invalidate_runtime_cache()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(repository.get, key)
        try:
            assert reader_checked.wait(5)
            writer = pool.submit(write)
            assert writer_started.wait(5)
        finally:
            continue_reader.set()
        assert reader.result(timeout=5) == expected
        writer.result(timeout=5)
    assert repository.get(key) == expected
