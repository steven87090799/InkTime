from __future__ import annotations

from statistics import median
from typing import Any

from inktime.app.repositories.devices import DeviceRepository


MAX_CHART_POINTS = 720
DEVICE_ENERGY_FIELDS = (
    "id",
    "name",
    "enabled",
    "firmware_version",
    "panel_profile",
    "last_status_at",
)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def _thin(samples: list[dict[str, Any]], maximum: int = MAX_CHART_POINTS) -> list[dict[str, Any]]:
    if len(samples) <= maximum:
        return samples
    indexes = {round(index * (len(samples) - 1) / (maximum - 1)) for index in range(maximum)}
    return [sample for index, sample in enumerate(samples) if index in indexes]


def _duration_summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "latest_seconds": values[-1] / 1000.0 if values else None,
        "average_seconds": sum(values) / len(values) / 1000.0 if values else None,
        "median_seconds": median(values) / 1000.0 if values else None,
        "p95_seconds": (
            value / 1000.0 if (value := _percentile(values, 0.95)) is not None else None
        ),
    }


def summarize_energy(samples: list[dict[str, Any]]) -> dict[str, Any]:
    battery_sample = next(
        (
            sample
            for sample in reversed(samples)
            if sample["battery_percent"] is not None or sample["battery_voltage"] is not None
        ),
        None,
    )
    refresh_durations = [
        float(sample["refresh_duration_ms"])
        for sample in samples
        if sample["refresh_duration_ms"] is not None and sample["refresh_duration_ms"] > 0
    ]
    wake_durations = [
        float(sample["wake_duration_ms"])
        for sample in samples
        if sample["wake_duration_ms"] is not None and sample["wake_duration_ms"] > 0
    ]
    warnings: list[str] = []
    if not samples:
        warnings.append("尚無能源遙測；新版韌體回報後才會建立曲線。")
    if battery_sample and battery_sample["battery_percent_estimated"]:
        warnings.append("電池百分比是裝置自動回報的診斷值，不會阻擋下載或刷新。")
    if battery_sample and _flag(battery_sample["usb_power"]) is True:
        warnings.append("最新樣本為 USB 供電；此欄位只用於判讀當時狀態。")
    if samples and battery_sample is None:
        warnings.append("裝置目前沒有可用的電池讀值；其他下載與刷新功能仍可繼續。")

    return {
        "latest": battery_sample,
        "refresh": _duration_summary(refresh_durations),
        "wake": _duration_summary(wake_durations),
        "warnings": warnings,
        "sample_count": len(samples),
        "battery_sample_count": sum(
            sample["battery_percent"] is not None or sample["battery_voltage"] is not None
            for sample in samples
        ),
    }


class DeviceEnergyService:
    def __init__(self, repository: DeviceRepository) -> None:
        self.repository = repository

    def dashboard(self, device_id: str, *, days: int = 30) -> dict[str, Any]:
        row = self.repository.get(device_id)
        if row is None:
            raise KeyError(device_id)
        device = {field: row[field] for field in DEVICE_ENERGY_FIELDS}
        samples = [dict(sample) for sample in self.repository.list_energy_samples(device_id, days=days)]
        chart_samples = [
            {
                "recorded_at": sample["recorded_at"],
                "battery_percent": _number(sample["battery_percent"]),
                "battery_voltage": _number(sample["battery_voltage"]),
                "refresh_seconds": (
                    float(sample["refresh_duration_ms"]) / 1000.0
                    if sample["refresh_duration_ms"] is not None
                    else None
                ),
                "wake_seconds": (
                    float(sample["wake_duration_ms"]) / 1000.0
                    if sample["wake_duration_ms"] is not None
                    else None
                ),
                "usb_power": (None if sample["usb_power"] is None else bool(sample["usb_power"])),
            }
            for sample in samples
        ]
        return {
            "device": device,
            "days": days,
            "samples": samples,
            "recent_samples": list(reversed(samples[-30:])),
            "chart_samples": _thin(chart_samples),
            "summary": summarize_energy(samples),
        }
