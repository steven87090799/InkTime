from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
from pathlib import Path
import sqlite3
import threading
import time

from PIL import Image
import pytest
import requests

from inktime.app.db.connection import ManagedConnection
from inktime.app.providers.base import ProviderResponse, Usage
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel
from inktime.app.repositories.jobs import PreviewCapacityError
from inktime.app.services.render_cache import BoundedRenderCache
from inktime.app.services.weather import WeatherService
from inktime.app.workers.job_worker import BoundedJobWorker
from inktime.app.workers.process_boundary import KillableProcessBoundary, ProcessCallError, ProcessCallTimeout


def _hang(_item):
    time.sleep(5)
    return {"stage": "too_late"}


def _hang_call(*, seconds: float):
    time.sleep(seconds)
    return "late"


def _return_call(*, value: str):
    return value


class _FaultyReceiver:
    def __init__(self, receiver, failure: str):
        self.receiver = receiver
        self.failure = failure

    def poll(self, *_args, **_kwargs):
        if self.failure == "poll":
            raise RuntimeError("simulated poll failure")
        return True

    def recv(self):
        if self.failure == "recv":
            raise RuntimeError("simulated recv failure")
        return self.receiver.recv()

    def close(self):
        self.receiver.close()
        raise RuntimeError("simulated close failure")


class _FaultContext:
    def __init__(self, failure: str):
        self.context = multiprocessing.get_context("spawn")
        self.failure = failure

    def Pipe(self, *, duplex=False):  # noqa: N802 - multiprocessing API
        receiver, sender = self.context.Pipe(duplex=duplex)
        return _FaultyReceiver(receiver, self.failure), sender

    def Process(self, **kwargs):  # noqa: N802 - multiprocessing API
        return self.context.Process(**kwargs)

    def get_start_method(self):
        return self.context.get_start_method()


class _StartFailureProcess:
    def start(self):
        raise RuntimeError("simulated start failure")


class _StartFailureContext:
    def __init__(self):
        self.context = multiprocessing.get_context("spawn")

    def Pipe(self, *, duplex=False):  # noqa: N802 - multiprocessing API
        return self.context.Pipe(duplex=duplex)

    def Process(self, **_kwargs):  # noqa: N802 - multiprocessing API
        return _StartFailureProcess()

    def get_start_method(self):
        return self.context.get_start_method()


class _ProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib callback name
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.dumps(
            {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return None


class _InvalidVisionResponseHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib callback name
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = b"not-json"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return None


class _ForbiddenParentSession:
    def post(self, *_args, **_kwargs):
        raise AssertionError("parent HTTP session must not be used by isolated child")

    def close(self):
        return None


class _CooperativeProvider(OpenAICompatibleProvider):
    def process_spec(self):
        return None

    def analyze(self, **_kwargs):
        return ProviderResponse("cooperative", Usage())


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
        "cooperative": 0,
    }
    boundary.shutdown()
    assert not [
        child for child in multiprocessing.active_children() if child.name == "inktime-provider-child"
    ]


def test_parent_cancel_callback_exception_still_reaps_child_and_releases_slot():
    boundary = KillableProcessBoundary(max_processes=1, terminate_grace_seconds=0.1)

    def broken_cancel():
        raise RuntimeError("simulated cancel callback failure")

    with pytest.raises(RuntimeError, match="cancel callback"):
        boundary.call(
            _hang_call,
            timeout_seconds=5,
            kwargs={"seconds": 5},
            cancel_requested=broken_cancel,
            process_name="inktime-cancel-fault-child",
        )
    assert boundary.observability()["active"] == 0
    assert boundary.call(_return_call, timeout_seconds=5, kwargs={"value": "reused"}) == "reused"
    boundary.shutdown()
    boundary.shutdown()
    assert not [
        child for child in multiprocessing.active_children() if child.name == "inktime-cancel-fault-child"
    ]


def test_child_start_failure_keeps_metrics_zero_and_releases_slot():
    boundary = KillableProcessBoundary(max_processes=1, terminate_grace_seconds=0.1)
    boundary._context = _StartFailureContext()
    with pytest.raises(RuntimeError, match="start failure"):
        boundary.call(
            _return_call,
            timeout_seconds=1,
            kwargs={"value": "never-started"},
        )
    assert boundary.observability() == {
        "active": 0,
        "active_max": 0,
        "timeout": 0,
        "terminated": 0,
        "cooperative": 0,
    }
    boundary._context = multiprocessing.get_context("spawn")
    assert boundary.call(_return_call, timeout_seconds=1, kwargs={"value": "reused"}) == "reused"


@pytest.mark.parametrize("failure", ["poll", "recv"])
def test_parent_pipe_exception_still_reaps_child(failure):
    boundary = KillableProcessBoundary(max_processes=1, terminate_grace_seconds=0.1)
    boundary._context = _FaultContext(failure)
    with pytest.raises(RuntimeError, match=failure):
        boundary.call(
            _hang_call,
            timeout_seconds=5,
            kwargs={"seconds": 5},
            process_name=f"inktime-{failure}-fault-child",
        )
    assert boundary.observability()["active"] == 0
    boundary._context = multiprocessing.get_context("spawn")
    assert boundary.call(_return_call, timeout_seconds=1, kwargs={"value": "reused"}) == "reused"
    boundary.shutdown()
    assert not [
        child for child in multiprocessing.active_children() if child.name == f"inktime-{failure}-fault-child"
    ]


def test_four_parent_threads_use_spawned_provider_children_without_inheriting_session(
    tmp_path: Path,
):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    image = tmp_path / "provider.jpg"
    Image.new("RGB", (8, 8), "white").save(image)
    provider = OpenAICompatibleProvider(
        name="spawn-test",
        base_url=f"http://127.0.0.1:{server.server_port}",
        api_key="",
        options={"allow_private_http": True},
        timeout=5,
        session=_ForbiddenParentSession(),
    )
    router = FailoverVisionProvider([ProviderChannel(provider=provider, max_concurrency=4)])
    boundary = KillableProcessBoundary(max_processes=2)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(
                executor.map(
                    lambda _index: router.analyze_isolated(
                        boundary,
                        image_path=image,
                        model="test-model",
                        detail="low",
                        stage="stage_one",
                    ),
                    range(4),
                )
            )
        assert [response.content for response in responses] == ["{}"] * 4
        assert boundary.start_method == "spawn"
        assert boundary.observability()["active_max"] <= 2
    finally:
        boundary.shutdown()
        server.shutdown()
        server.server_close()
    assert not [
        child for child in multiprocessing.active_children() if child.name == "inktime-provider-child"
    ]


def test_isolated_vision_failure_preserves_no_failover_metadata(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _InvalidVisionResponseHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    image = tmp_path / "provider-invalid.jpg"
    Image.new("RGB", (8, 8), "white").save(image)
    provider = OpenAICompatibleProvider(
        name="isolated-invalid",
        base_url=f"http://127.0.0.1:{server.server_port}",
        api_key="",
        options={"allow_private_http": True},
        timeout=5,
        session=_ForbiddenParentSession(),
    )
    router = FailoverVisionProvider([ProviderChannel(provider=provider)])
    boundary = KillableProcessBoundary(max_processes=1)
    try:
        with pytest.raises(ProcessCallError) as raised:
            router.analyze_isolated(
                boundary,
                image_path=image,
                model="test-model",
                detail="low",
                stage="stage_one",
            )
        assert raised.value.vision_started is True
        assert raised.value.ambiguous is True
    finally:
        boundary.shutdown()
        server.shutdown()
        server.server_close()


def test_provider_without_serializable_spec_uses_cooperative_timeout():
    provider = _CooperativeProvider(
        name="cooperative",
        base_url="https://unused.invalid",
        api_key="",
    )
    router = FailoverVisionProvider([ProviderChannel(provider=provider)])
    boundary = KillableProcessBoundary(max_processes=1)
    response = router.analyze_isolated(
        boundary,
        image_path=Path("unused.jpg"),
        model="test",
        detail="low",
        stage="stage_one",
    )
    assert response.content == "cooperative"
    assert boundary.observability()["active_max"] == 0
    assert boundary.observability()["cooperative"] == 1


def test_preview_capacity_is_atomic_for_twenty_requests_from_one_user(app):
    repository = app.extensions["inktime_job_repository"]

    def create(index: int) -> str:
        try:
            return repository.create_maintenance_with_capacity(
                kind="render_preview",
                name=f"preview-{index}",
                settings={},
                created_by="same-user",
                priority=6,
                per_user_limit=2,
                system_limit=8,
            )
        except PreviewCapacityError as exc:
            return exc.scope

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(create, range(20)))
    assert results.count("user") == 18
    assert repository.active_count("render_preview", created_by="same-user") == 2
    assert repository.active_count("render_preview") == 2


def test_preview_capacity_is_atomic_for_twenty_distinct_users(app):
    repository = app.extensions["inktime_job_repository"]

    def create(index: int) -> str:
        try:
            return repository.create_maintenance_with_capacity(
                kind="render_preview",
                name=f"preview-{index}",
                settings={},
                created_by=f"user-{index}",
                priority=6,
                per_user_limit=2,
                system_limit=8,
            )
        except PreviewCapacityError as exc:
            return exc.scope

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(create, range(20)))
    assert results.count("system") == 12
    assert repository.active_count("render_preview") == 8


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

    settings.update("render.weather_latitude", 24.15, changed_by="test", source_ip="local")
    separated = service.current()
    assert separated and separated["available"] is True
    assert session.calls == 3


def test_weather_cold_failure_retry_ttl_suppresses_repeated_calls(app):
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.weather_enabled", True, changed_by="test", source_ip="local")
    session = _FailingWeatherSession()
    service = WeatherService(settings, session=session, failure_retry_ttl=timedelta(hours=1))
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
