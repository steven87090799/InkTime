from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Condition, Lock
from typing import Any

import requests

from inktime.app.repositories.settings import SettingsRepository


WEATHER_LABELS = {
    0: "晴朗",
    1: "大致晴朗",
    2: "局部多雲",
    3: "多雲",
    45: "有霧",
    48: "霧淞",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "較強毛毛雨",
    56: "凍毛毛雨",
    57: "較強凍毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "凍雨",
    67: "強凍雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "霰",
    80: "短暫小雨",
    81: "短暫陣雨",
    82: "強陣雨",
    85: "短暫小雪",
    86: "強陣雪",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴大冰雹",
}


@dataclass
class _WeatherCacheEntry:
    value: dict[str, Any] | None = None
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    fresh_until: datetime = datetime.min.replace(tzinfo=timezone.utc)
    stale_until: datetime = datetime.min.replace(tzinfo=timezone.utc)
    retry_after: datetime = datetime.min.replace(tzinfo=timezone.utc)
    refreshing: bool = False


class WeatherService:
    """低頻取得相框天氣；失敗只降級版型，不阻止照片發布。"""

    endpoint = "https://api.open-meteo.com/v1/forecast"

    def __init__(
        self,
        settings: SettingsRepository,
        session: requests.Session | None = None,
        *,
        fresh_ttl: timedelta = timedelta(minutes=30),
        stale_ttl: timedelta = timedelta(hours=6),
        failure_retry_ttl: timedelta = timedelta(minutes=5),
        wait_seconds: float = 1.0,
        max_entries: int = 128,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._cache: OrderedDict[tuple[float, float, str, str], _WeatherCacheEntry] = (
            OrderedDict()
        )
        self.fresh_ttl = fresh_ttl
        self.stale_ttl = max(stale_ttl, fresh_ttl)
        self.failure_retry_ttl = failure_retry_ttl
        self.wait_seconds = max(0.0, wait_seconds)
        self.max_entries = max(1, max_entries)
        self._metrics = {
            "fresh": 0,
            "stale": 0,
            "refresh": 0,
            "failure": 0,
        }

    @staticmethod
    def _unavailable(exc: Exception | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "available": False,
            "condition": "天氣暫時無法取得",
        }
        if exc is not None:
            result["error"] = type(exc).__name__
        return result

    def observability(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)

    def _trim_locked(self, now: datetime) -> None:
        expired = [
            key
            for key, entry in self._cache.items()
            if not entry.refreshing
            and entry.stale_until <= now
            and entry.retry_after <= now
        ]
        for key in expired:
            self._cache.pop(key, None)
        while len(self._cache) > self.max_entries:
            removable = next(
                (key for key, entry in self._cache.items() if not entry.refreshing), None
            )
            if removable is None:
                break
            self._cache.pop(removable, None)

    def current(self) -> dict[str, Any] | None:
        if not bool(self.settings.get("render.weather_enabled", False)):
            return None
        latitude = float(self.settings.get("render.weather_latitude", 25.033))
        longitude = float(self.settings.get("render.weather_longitude", 121.5654))
        timezone_name = str(self.settings.get("general.timezone", "Asia/Taipei"))
        provider = "open-meteo"
        location = (latitude, longitude, timezone_name, provider)
        now = datetime.now(timezone.utc)
        with self._condition:
            self._trim_locked(now)
            entry = self._cache.setdefault(location, _WeatherCacheEntry())
            self._cache.move_to_end(location)
            if entry.value is not None and now < entry.fresh_until:
                self._metrics["fresh"] += 1
                return dict(entry.value)
            if entry.refreshing:
                if entry.value is not None and now < entry.stale_until:
                    self._metrics["stale"] += 1
                    return dict(entry.value)
                self._condition.wait_for(
                    lambda: not entry.refreshing, timeout=self.wait_seconds
                )
                now = datetime.now(timezone.utc)
                if entry.value is not None and now < entry.stale_until:
                    self._metrics["stale"] += 1
                    return dict(entry.value)
                if entry.refreshing:
                    self._metrics["failure"] += 1
                    return self._unavailable()
            if now < entry.retry_after:
                if entry.value is not None and now < entry.stale_until:
                    self._metrics["stale"] += 1
                    return dict(entry.value)
                self._metrics["failure"] += 1
                return self._unavailable()
            entry.refreshing = True
            entry.last_attempt_at = now
            self._metrics["refresh"] += 1
        result: dict[str, Any] | None = None
        failure: Exception | None = None
        try:
            params: dict[str, str | int | float] = {
                "latitude": latitude,
                "longitude": longitude,
                "timezone": timezone_name,
                "forecast_days": 1,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            }
            response = self.session.get(
                self.endpoint,
                params=params,
                timeout=5,
                headers={"User-Agent": "InkTime/2.4 weather-frame"},
            )
            response.raise_for_status()
            payload = response.json()
            current = payload.get("current") or {}
            daily = payload.get("daily") or {}
            code = int(current.get("weather_code", (daily.get("weather_code") or [0])[0]))
            candidate = {
                "available": True,
                "temperature_c": float(current["temperature_2m"]),
                "humidity_percent": float(current["relative_humidity_2m"]),
                "apparent_temperature_c": float(current["apparent_temperature"]),
                "minimum_c": float((daily.get("temperature_2m_min") or [0])[0]),
                "maximum_c": float((daily.get("temperature_2m_max") or [0])[0]),
                "weather_code": code,
                "condition": WEATHER_LABELS.get(code, "天氣狀況未知"),
                "observed_at": str(current.get("time", "")),
            }
            # Empty or incomplete responses never replace a usable stale value.
            if not candidate.get("observed_at"):
                raise ValueError("weather response has no observation time")
            result = candidate
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            failure = exc
        completed = datetime.now(timezone.utc)
        with self._condition:
            entry = self._cache.setdefault(location, entry)
            if result is not None:
                entry.value = result
                entry.last_success_at = completed
                entry.fresh_until = completed + self.fresh_ttl
                entry.stale_until = completed + self.stale_ttl
                entry.retry_after = completed
            else:
                self._metrics["failure"] += 1
                entry.retry_after = completed + self.failure_retry_ttl
            entry.refreshing = False
            self._cache.move_to_end(location)
            self._trim_locked(completed)
            self._condition.notify_all()
            if entry.value is not None and completed < entry.stale_until:
                if failure is not None:
                    self._metrics["stale"] += 1
                return dict(entry.value)
        return self._unavailable(failure)
