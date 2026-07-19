from __future__ import annotations

import pytest

from inktime.app.services.device_energy import summarize_energy


def _device(**overrides):
    return {
        "battery_capacity_mah": 5000.0,
        "standby_current_ma": 0.1,
        "active_current_ma": 200.0,
        "refreshes_per_day": 1.0,
        "battery_reserve_percent": 10.0,
    } | overrides


def _sample(day: int, percent: float, **overrides):
    return {
        "recorded_at": f"2026-07-{day:02d}T00:00:00+00:00",
        "battery_voltage": 3.7 + percent / 1000,
        "battery_percent": percent,
        "battery_percent_estimated": 1,
        "usb_power": 0,
        "refresh_duration_ms": 25_000,
        "wake_duration_ms": 60_000,
        "display_updated": 1,
        "temperature_c": 25.0,
        "wifi_rssi": -60,
        "wake_reason": "timer",
    } | overrides


def test_energy_summary_prefers_observed_discharge_after_two_days():
    summary = summarize_energy(
        _device(),
        [_sample(1, 80), _sample(2, 78), _sample(3, 76)],
    )

    assert summary["empirical"]["daily_percent_drop"] == pytest.approx(2.0)
    assert summary["empirical"]["remaining_days"] == pytest.approx(33.0)
    assert summary["empirical"]["confidence"] == "medium"
    assert summary["preferred"]["method"] == "empirical"
    assert summary["refresh"]["average_seconds"] == pytest.approx(25.0)


def test_energy_model_uses_whole_wake_cycle_and_manual_currents():
    summary = summarize_energy(_device(), [_sample(1, 80)])

    modeled = summary["modeled"]
    assert modeled["duration_source"] == "wake_cycle"
    assert modeled["active_seconds"] == pytest.approx(60.0)
    assert modeled["daily_mah"] == pytest.approx(5.7317, rel=1e-3)
    assert modeled["remaining_days"] == pytest.approx(610.64, rel=1e-3)
    assert summary["preferred"]["method"] == "modeled"


def test_energy_summary_does_not_treat_usb_samples_as_battery_discharge():
    summary = summarize_energy(
        _device(standby_current_ma=None),
        [_sample(1, 80), _sample(2, 75, usb_power=1)],
    )

    assert summary["empirical"] is None
    assert summary["modeled"] is None
    assert summary["preferred"] is None
    assert "待機電流" in summary["missing_model_inputs"]
    assert any("USB 供電" in warning for warning in summary["warnings"])
