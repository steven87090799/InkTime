from __future__ import annotations

from inktime.app.services.device_energy import summarize_energy


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


def test_energy_summary_reports_only_automatic_runtime_observations():
    summary = summarize_energy([_sample(1, 80), _sample(2, 78), _sample(3, 76)])

    assert summary["refresh"]["average_seconds"] == 25.0
    assert summary["wake"]["average_seconds"] == 60.0
    assert summary["sample_count"] == 3
    assert "modeled" not in summary
    assert "empirical" not in summary
    assert "preferred" not in summary


def test_energy_summary_labels_usb_sample_as_diagnostic():
    summary = summarize_energy([_sample(1, 80), _sample(2, 75, usb_power=1)])

    assert any("USB 供電" in warning for warning in summary["warnings"])


def test_missing_battery_telemetry_does_not_request_manual_measurement():
    sample = _sample(1, 0, battery_percent=None, battery_voltage=None)
    summary = summarize_energy([sample])

    assert summary["latest"] is None
    assert any("下載與刷新功能仍可繼續" in warning for warning in summary["warnings"])
