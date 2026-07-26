from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import multiprocessing
from pathlib import Path
import sqlite3
import time

from PIL import Image
import requests

from inktime.app.db.connection import ManagedConnection
from inktime.app.services.render_cache import BoundedRenderCache
from inktime.app.services.weather import WeatherService
from inktime.app.workers.job_worker import BoundedJobWorker
from inktime.app.workers.process_boundary import KillableProcessBoundary, ProcessCallTimeout


def _hang(_item):
    time.sleep(5)
    return {"stage": "too_late"}


def _hang_call(*, seconds: float):
    time.sleep(seconds)
    return "late"


def test_hard_timeout_terminates_joins_and_rejects_late_result(app):
    repository = app.extensions["inktime_job_repository"]
    job_id = repository.create_maintenance(
        kind="cleanup", name="hard timeout", settings={}, created_by="test"
    )
    app.extensions["inktime_job_service"].start(job_id)
    worker = BoundedJobWorker(
        repository,
        _hang,
        concurrency=1,
        queue_multiplier=1,
        timeout_seconds=0.1,
        hard_timeout=True,
        max_attempts=1,
    )

    started = time.monotonic()
    worker.run_job(job_id)

    assert time.monotonic() - started < 2
    item = repository.list_items(job_id)[0]
    assert item["status"] == "failed"
    assert item["error_code"] == "JOB-004"
    assert item["result_json"] is None
    assert worker.child_observability() == {
        "active": 0,
        "active_max": 1,
        "timeouts": 1,
        "terminated": 1,
    }
    assert not [child for child in multiprocessing.active_children() if child.name == "inktime-bounded-child"]


def test_provider_call_process_boundary_has_hard_cap_and_shutdown_cleanup():
    boundary = KillableProcessBoundary(max_processes=1, terminate_grace_seconds=0.1)
    try:
        boundary.call(_hang_call, timeout_seconds=0.1, kwargs={"seconds": 5})
    except ProcessCallTimeout:
        pass
    else:
        raise AssertionError("provider child should time out")
    assert boundary.observability() == {
        "active": 0,
        "active_max": 1,
        "timeout": 1,
        "terminated": 1,
    }
    boundary.shutdown()
    assert not [child for child in multiprocessing.active_children() if child.name == "inktime-provider-child"]


class _WeatherResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "current": {
                "time": "2026-07-26T12:00",
                "temperature_2m": 30,
                "relative_humidity_2m": 70,
                "apparent_temperature": 34,
                "weather_code": 1,
            },
            "daily": {
                "temperature_2m_min": [26],
                "temperature_2m_max": [33],
                "weather_code": [1],
            },
        }


class _SlowWeatherSession:
    def __init__(self):
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        time.sleep(0.05)
        return _WeatherResponse()


def test_weather_singleflight_serves_twenty_threads_with_one_refresh(app):
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.weather_enabled", True, changed_by="test", source_ip="local")
    session = _SlowWeatherSession()
    service = WeatherService(settings, session=session, wait_seconds=1)

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _index: service.current(), range(20)))

    assert session.calls == 1
    assert all(result and result["temperature_c"] == 30 for result in results)
    assert service.observability()["refresh"] == 1


class _WeatherSequence:
    def __init__(self):
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 2:
            raise requests.Timeout("private details must not replace stale")
        return _WeatherResponse()


class _FailingWeatherSession:
    def __init__(self):
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        raise requests.Timeout("unavailable")


def test_weather_failure_keeps_last_success_and_honors_failure_ttl_and_key(app):
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.weather_enabled", True, changed_by="test", source_ip="local")
    session = _WeatherSequence()
    service = WeatherService(
        settings,
        session=session,
        fresh_ttl=timedelta(0),
        stale_ttl=timedelta(hours=1),
        failure_retry_ttl=timedelta(hours=1),
    )
    first = service.current()
    stale = service.current()
    retry_suppressed = service.current()
    assert stale == first == retry_suppressed
    assert session.calls == 2

    settings.update(
        "render.weather_latitude", 24.15, changed_by="test", source_ip="local"
    )
    separated = service.current()
    assert separated and separated["available"] is True
    assert session.calls == 3


def test_weather_cold_failure_retry_ttl_suppresses_repeated_calls(app):
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.weather_enabled", True, changed_by="test", source_ip="local")
    session = _FailingWeatherSession()
    service = WeatherService(
        settings, session=session, failure_retry_ttl=timedelta(hours=1)
    )
    assert service.current()["available"] is False
    assert service.current()["available"] is False
    assert session.calls == 1


def test_sql_detection_covers_comments_cte_scripts_and_write_pragma():
    requires = ManagedConnection._requires_writer
    assert requires("-- comment\n INSERT INTO example VALUES (1)")
    assert requires("/* comment */ WITH x AS (SELECT 1) UPDATE example SET value=1")
    assert requires("WITH x AS (SELECT 1) DELETE FROM example")
    assert requires("PRAGMA journal_mode=WAL")
    assert requires("SELECT 1; /* next */ CREATE TABLE example(id INTEGER)")
    assert not requires("/* comment */ WITH x AS (SELECT 1) SELECT * FROM x")
    assert not requires("WITH x AS (SELECT 'update') SELECT * FROM x")
    assert not requires("PRAGMA integrity_check")
    assert not requires("PRAGMA table_info(example)")


def test_writer_backoff_uses_bounded_jitter(monkeypatch, tmp_path: Path):
    connection = sqlite3.connect(":memory:", factory=ManagedConnection)
    metrics = {
        "writer_lock_acquisitions": 0,
        "writer_lock_wait_count": 0,
        "writer_lock_wait_ms": 0.0,
        "writer_lock_wait_max_ms": 0.0,
        "writer_lock_backoff_ms": 0.0,
        "writer_lock_wait_le_10ms": 0,
        "writer_lock_wait_le_50ms": 0,
        "writer_lock_wait_le_250ms": 0,
        "busy_timeout_count": 0,
    }
    import threading
    from inktime.app.db import connection as connection_module

    connection.configure_writer_lock(tmp_path / "writer.lock", 1, metrics, threading.Lock())
    original = connection_module.fcntl.flock
    calls = 0
    sleeps = []

    def flaky(*args):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise BlockingIOError()
        return original(*args)

    monkeypatch.setattr(connection_module.fcntl, "flock", flaky)
    monkeypatch.setattr(connection_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(connection_module.random, "uniform", lambda low, high: high)
    connection.execute("CREATE TABLE example(id INTEGER)")
    connection.close()

    assert sleeps == [0.005, 0.01]
    assert metrics["writer_lock_wait_count"] == 2
    assert metrics["writer_lock_wait_le_10ms"] == 2


def test_renderer_cache_fingerprint_is_bounded_atomic_and_corruption_safe(tmp_path: Path):
    cache = BoundedRenderCache(
        tmp_path / "cache", max_entries=2, max_bytes=2 * 1024 * 1024, retention=timedelta(days=1)
    )
    base = {
        "photo_sha": "a",
        "effective_orientation": 0,
        "orientation_source": "none",
        "manual_orientation_updated_at": None,
        "crop": [0.5, 0.5],
        "fit": "contain",
        "layout": "photo_info",
        "secondary_photo_sha": None,
        "profile": "safe_4c",
        "panel_profile": "safe_4c",
        "palette": "safe_4c",
        "dither": "nearest",
        "strength": 1.0,
        "preset": "photo_info",
        "renderer_version": "v1",
        "font_version": "font",
        "output_dimensions": [480, 800],
    }
    cache.put(base, Image.new("RGB", (20, 20), "white"))
    assert cache.get(base) is not None
    changed = dict(base, crop=[0.2, 0.5])
    assert cache.get(changed) is None
    path = cache.root / f"{cache.fingerprint(base)}.png"
    path.write_bytes(b"broken")
    assert cache.get(base) is None
    assert not path.exists()
