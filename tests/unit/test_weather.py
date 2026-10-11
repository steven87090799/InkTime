from __future__ import annotations

from datetime import timedelta

import pytest

from inktime.app.services.weather import WeatherService


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "current": {
                "time": "2026-07-20T12:00",
                "temperature_2m": 31.2,
                "relative_humidity_2m": 68,
                "apparent_temperature": 35.1,
                "weather_code": 2,
            },
            "daily": {
                "temperature_2m_min": [27.0],
                "temperature_2m_max": [34.0],
                "weather_code": [2],
            },
        }


class _Session:
    def __init__(self):
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return _Response()


def test_weather_service_is_opt_in_cached_and_returns_traditional_chinese(app):
    settings = app.extensions["inktime_settings_repository"]
    session = _Session()
    service = WeatherService(settings, session=session)
    assert service.current() is None
    settings.update("render.weather_enabled", True, changed_by="test", source_ip="127.0.0.1")

    first = service.current()
    second = service.current()

    assert first["condition"] == "局部多雲"
    assert first["temperature_c"] == 31.2
    assert second == first
    assert session.calls == 1


@pytest.mark.parametrize("invalid", [[], {"current": [1]}, {"daily": "invalid"},
    {"daily": {"temperature_2m_min": []}}, {"current": {"temperature_2m": float("nan")}},
    {"current": {"weather_code": float("inf")}}])
def test_invalid_weather_keeps_stale_data_and_allows_next_refresh(app, invalid):
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.weather_enabled", True, changed_by="test", source_ip="127.0.0.1")
    valid = _Response().json()
    if isinstance(invalid, dict):
        malformed = {key: dict(value) for key, value in valid.items()}
        for key, value in invalid.items():
            if isinstance(value, dict):
                malformed[key].update(value)
            else:
                malformed[key] = value
    else:
        malformed = invalid

    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            payload = malformed if self.calls == 2 else valid

            class Response(_Response):
                def json(self):
                    return payload

            return Response()

    session = Session()
    service = WeatherService(settings, session=session, fresh_ttl=timedelta(0), failure_retry_ttl=timedelta(0))
    first = service.current()
    assert first["available"] is True
    assert service.current() == first
    assert service.current() == first
    assert session.calls == 3
