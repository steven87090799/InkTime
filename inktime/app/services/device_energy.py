from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

from inktime.app.repositories.devices import DeviceRepository


SECONDS_PER_DAY = 86_400.0
MAX_CHART_POINTS = 720
DEVICE_ENERGY_FIELDS = (
    "id",
    "name",
    "enabled",
    "firmware_version",
    "panel_profile",
    "last_status_at",
    "battery_capacity_mah",
    "standby_current_ma",
    "active_current_ma",
    "refreshes_per_day",
    "battery_reserve_percent",
    "energy_profile_updated_at",
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


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


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


def _empirical_estimate(
    samples: list[dict[str, Any]], current_percent: float | None, reserve_percent: float
) -> dict[str, Any] | None:
    battery_samples = [
        sample
        for sample in samples
        if sample["battery_percent"] is not None and _flag(sample["usb_power"]) is False
    ]
    if len(battery_samples) < 2 or current_percent is None:
        return None

    rates: list[float] = []
    for index in range(1, len(battery_samples)):
        previous = battery_samples[index - 1]
        current = battery_samples[index]
        elapsed_days = (
            _timestamp(current["recorded_at"]) - _timestamp(previous["recorded_at"])
        ) / SECONDS_PER_DAY
        percent_drop = float(previous["battery_percent"]) - float(current["battery_percent"])
        if elapsed_days >= 0.25 and 0.05 <= percent_drop <= 50:
            rate = percent_drop / elapsed_days
            if rate <= 100:
                rates.append(rate)
    if not rates:
        return None

    span_days = (
        _timestamp(battery_samples[-1]["recorded_at"])
        - _timestamp(battery_samples[0]["recorded_at"])
    ) / SECONDS_PER_DAY
    drain_percent_per_day = median(rates)
    remaining_percent = max(0.0, current_percent - reserve_percent)
    confidence = "low"
    if span_days >= 7 and len(rates) >= 5:
        confidence = "high"
    elif span_days >= 2 and len(rates) >= 2:
        confidence = "medium"
    return {
        "method": "empirical",
        "remaining_days": remaining_percent / drain_percent_per_day,
        "full_days": max(0.0, 100.0 - reserve_percent) / drain_percent_per_day,
        "daily_percent_drop": drain_percent_per_day,
        "confidence": confidence,
        "observed_days": span_days,
        "intervals": len(rates),
    }


def _modeled_estimate(
    device: dict[str, Any],
    current_percent: float | None,
    refresh_durations: list[float],
    wake_durations: list[float],
) -> tuple[dict[str, Any] | None, list[str]]:
    capacity = _number(device.get("battery_capacity_mah"))
    standby_current = _number(device.get("standby_current_ma"))
    active_current = _number(device.get("active_current_ma"))
    refreshes_per_day = _number(device.get("refreshes_per_day")) or 1.0
    reserve_percent = _number(device.get("battery_reserve_percent")) or 0.0
    missing: list[str] = []
    if capacity is None:
        missing.append("電池容量")
    if standby_current is None:
        missing.append("待機電流")
    if active_current is None:
        missing.append("喚醒週期平均電流")

    duration_source = "wake_cycle"
    if wake_durations:
        active_seconds = median(wake_durations) / 1000.0
    elif refresh_durations:
        active_seconds = median(refresh_durations) / 1000.0
        duration_source = "refresh_only"
    else:
        active_seconds = 0.0
        missing.append("喚醒／刷新耗時樣本")
    if missing or capacity is None or standby_current is None or active_current is None:
        return None, missing

    active_hours = active_seconds * refreshes_per_day / 3600.0
    if active_hours <= 0 or active_hours >= 24:
        return None, ["每日喚醒時間不合理"]
    standby_hours = 24.0 - active_hours
    daily_mah = standby_current * standby_hours + active_current * active_hours
    if daily_mah <= 0:
        return None, ["每日耗電必須大於零"]

    usable_full_mah = capacity * max(0.0, 100.0 - reserve_percent) / 100.0
    remaining_days = None
    if current_percent is not None:
        remaining_mah = capacity * max(0.0, current_percent - reserve_percent) / 100.0
        remaining_days = remaining_mah / daily_mah
    return (
        {
            "method": "modeled",
            "remaining_days": remaining_days,
            "full_days": usable_full_mah / daily_mah,
            "daily_mah": daily_mah,
            "active_seconds": active_seconds,
            "duration_source": duration_source,
            "refreshes_per_day": refreshes_per_day,
            "reserve_percent": reserve_percent,
        },
        [],
    )


def summarize_energy(device: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
    battery_sample = next(
        (
            sample
            for sample in reversed(samples)
            if sample["battery_percent"] is not None or sample["battery_voltage"] is not None
        ),
        None,
    )
    current_percent = battery_sample["battery_percent"] if battery_sample else None
    reserve_percent = _number(device.get("battery_reserve_percent")) or 0.0
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
    empirical = _empirical_estimate(samples, current_percent, reserve_percent)
    modeled, missing_model_inputs = _modeled_estimate(
        device, current_percent, refresh_durations, wake_durations
    )
    preferred = None
    if empirical and empirical["confidence"] in {"medium", "high"}:
        preferred = empirical
    elif modeled:
        preferred = modeled
    elif empirical:
        preferred = empirical

    warnings: list[str] = []
    if not samples:
        warnings.append("尚無能源遙測；新版韌體回報後才會建立曲線。")
    if battery_sample and battery_sample["battery_percent_estimated"]:
        warnings.append("電池百分比由 PMIC／電壓曲線估算，尚未以實機放電曲線校正。")
    if battery_sample and _flag(battery_sample["usb_power"]) is True:
        warnings.append("最新樣本為 USB 供電；放電趨勢只採用明確的電池模式樣本。")
    if missing_model_inputs:
        warnings.append("續航模型尚缺：" + "、".join(missing_model_inputs) + "。")
    if modeled and modeled["duration_source"] == "refresh_only":
        warnings.append("目前以刷新耗時代替完整喚醒週期，尚未涵蓋 Wi-Fi 與下載時間。")

    return {
        "latest": battery_sample,
        "refresh": {
            "count": len(refresh_durations),
            "latest_seconds": refresh_durations[-1] / 1000.0 if refresh_durations else None,
            "average_seconds": (
                sum(refresh_durations) / len(refresh_durations) / 1000.0
                if refresh_durations
                else None
            ),
            "median_seconds": median(refresh_durations) / 1000.0 if refresh_durations else None,
            "p95_seconds": (
                value / 1000.0 if (value := _percentile(refresh_durations, 0.95)) is not None else None
            ),
        },
        "empirical": empirical,
        "modeled": modeled,
        "preferred": preferred,
        "missing_model_inputs": missing_model_inputs,
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
                "usb_power": (
                    None if sample["usb_power"] is None else bool(sample["usb_power"])
                ),
            }
            for sample in samples
        ]
        return {
            "device": device,
            "days": days,
            "samples": samples,
            "recent_samples": list(reversed(samples[-30:])),
            "chart_samples": _thin(chart_samples),
            "summary": summarize_energy(device, samples),
        }
