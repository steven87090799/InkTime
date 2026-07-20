from __future__ import annotations

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
    settings.update(
        "render.weather_enabled", True, changed_by="test", source_ip="127.0.0.1"
    )

    first = service.current()
    second = service.current()

    assert first["condition"] == "局部多雲"
    assert first["temperature_c"] == 31.2
    assert second == first
    assert session.calls == 1
