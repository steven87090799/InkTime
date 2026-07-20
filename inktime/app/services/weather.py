from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock
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


class WeatherService:
    """低頻取得相框天氣；失敗只降級版型，不阻止照片發布。"""

    endpoint = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, settings: SettingsRepository, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self._lock = Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_until = datetime.min.replace(tzinfo=timezone.utc)
        self._cached_location: tuple[float, float, str] | None = None

    def current(self) -> dict[str, Any] | None:
        if not bool(self.settings.get("render.weather_enabled", False)):
            return None
        latitude = float(self.settings.get("render.weather_latitude", 25.033))
        longitude = float(self.settings.get("render.weather_longitude", 121.5654))
        timezone_name = str(self.settings.get("general.timezone", "Asia/Taipei"))
        location = (latitude, longitude, timezone_name)
        now = datetime.now(timezone.utc)
        with self._lock:
            if self._cached_location == location and now < self._cached_until:
                return dict(self._cached or {})
        try:
            response = self.session.get(
                self.endpoint,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "timezone": timezone_name,
                    "forecast_days": 1,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                },
                timeout=5,
                headers={"User-Agent": "InkTime/2.4 weather-frame"},
            )
            response.raise_for_status()
            payload = response.json()
            current = payload.get("current") or {}
            daily = payload.get("daily") or {}
            code = int(current.get("weather_code", (daily.get("weather_code") or [0])[0]))
            result = {
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
            ttl = timedelta(minutes=30)
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            result = {
                "available": False,
                "condition": "天氣暫時無法取得",
                "error": type(exc).__name__,
            }
            ttl = timedelta(minutes=5)
        with self._lock:
            self._cached = result
            self._cached_location = location
            self._cached_until = now + ttl
        return dict(result)
