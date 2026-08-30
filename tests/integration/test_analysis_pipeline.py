from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from PIL import Image

import pytest

from inktime.app.domain.photos import PhotoPreprocessor, ThumbnailCache
from inktime.app.domain.jobs.failure_policy import FailureClass, classify_failure
from inktime.app.providers.base import ProviderCallTrace, ProviderResponse, Usage, VisionProvider
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError
from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.analysis import PhotoAnalysisService
from inktime.app.services.budgets import BudgetExceeded, BudgetService
from inktime.app.domain.analysis.plan import build_analysis_plan, fingerprint
from inktime.app.domain.analysis.schema import AnalysisValidationError
from inktime.app.workers.job_worker import BoundedJobWorker
from inktime.app.workers.scanner import PhotoScanner
from inktime.app.workers.process_boundary import KillableProcessBoundary, ProcessCallError
from tests.conftest import create_admin
from tests.unit.test_analysis_schema import valid_result


class MockProvider(VisionProvider):
    name = "Mock Provider"

    def __init__(self, responses):
        self.responses = list(responses)
        self.analyze_calls = 0
        self.analyze_kwargs: list[dict] = []
        self.repair_calls = 0
        self.repair_kwargs: list[dict] = []

    def analyze(self, **kwargs):
        self.analyze_calls += 1
        self.analyze_kwargs.append(kwargs)
        value = self.responses.pop(0)
        return ProviderResponse(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), Usage(1000, 100, 0)
        )

    def repair_json(self, **kwargs):
        self.repair_calls += 1
        self.repair_kwargs.append(kwargs)
        value = self.responses.pop(0)
        return ProviderResponse(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), Usage(200, 100, 0)
        )

    def submit_batch(self, requests, completion_window="24h"):
        return "batch"

    def poll_batch(self, batch_id):
        return {"status": "completed"}

    def cancel_batch(self, batch_id):
        return {"status": "cancelled"}

    def estimate_cost(self, model, usage):
        return (usage.input_tokens + usage.output_tokens) / 1_000_000

    def validate_config(self):
        return True, "ok"


class FailingProvider(MockProvider):
    name = "Failing Provider"

    def analyze(self, **kwargs):
        self.analyze_calls += 1
        raise RuntimeError("provider unavailable")


class AmbiguousProvider(MockProvider):
    name = "Ambiguous Provider"

    def analyze(self, **kwargs):
        self.analyze_calls += 1
        now = datetime.now(timezone.utc).isoformat()
        raise ProviderHTTPError(
            "response lost after vision POST",
            "VLM-AMBIGUOUS",
            ambiguous=True,
            call_trace=ProviderCallTrace(
                request_built_at=now,
                request_started_at=now,
                endpoint="https://provider.invalid/chat/completions",
            ),
        )


class _BoundaryHTTPState:
    def __init__(self, mode: str):
        self.mode = mode
        self.lock = threading.Lock()
        self.vision_requests = 0
        self.repair_requests = 0
        self.release_blocked_response = threading.Event()

    def record(self, *, image_request: bool) -> None:
        with self.lock:
            if image_request:
                self.vision_requests += 1
            else:
                self.repair_requests += 1


class _BoundaryHTTPHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib callback name
        state: _BoundaryHTTPState = self.server.state
        request_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        request = json.loads(request_body)
        messages = request.get("messages") if isinstance(request, dict) else None
        user_message = messages[1] if isinstance(messages, list) and len(messages) > 1 else {}
        content = user_message.get("content") if isinstance(user_message, dict) else None
        image_request = isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_url" for part in content
        )
        state.record(image_request=image_request)

        if (
            state.mode == "vision_timeout" and image_request
        ) or (
            state.mode == "invalid_then_repair_timeout" and not image_request
        ):
            state.release_blocked_response.wait()

        status_code = 200
        if state.mode == "vision_429" and image_request:
            status_code = 429
        elif state.mode == "vision_5xx" and image_request:
            status_code = 503
        elif state.mode == "invalid_then_repair_429" and not image_request:
            status_code = 429
        elif state.mode == "invalid_then_repair_redirect" and not image_request:
            status_code = 307
        elif state.mode == "invalid_then_repair_5xx" and not image_request:
            status_code = 503

        if state.mode.startswith("invalid_then_repair") and image_request:
            response_content = "not-json"
        else:
            response_content = json.dumps(valid_result(), ensure_ascii=False)
        if status_code >= 400:
            response_content = json.dumps({"error": {"type": "test-provider-error"}}, ensure_ascii=False)
        response_body = json.dumps(
            {
                "choices": [{"message": {"content": response_content}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        except OSError:
            # The parent intentionally kills the child during timeout tests;
            # the server may then be writing to the closed child socket.
            pass

    def log_message(self, _format, *_args):
        return None


class _ShortProviderBoundary(KillableProcessBoundary):
    def __init__(self, *, timeout_seconds: float = 2.0):
        super().__init__(max_processes=1, terminate_grace_seconds=0.05)
        self.timeout_seconds = timeout_seconds

    def call_provider(self, specification, method, *, timeout_seconds, kwargs):
        return super().call_provider(
            specification,
            method,
            timeout_seconds=min(float(timeout_seconds), self.timeout_seconds),
            kwargs=kwargs,
        )


class _RepairCapacityBoundary(_ShortProviderBoundary):
    def call_provider(self, specification, method, *, timeout_seconds, kwargs):
        if method != "repair_json":
            return super().call_provider(
                specification,
                method,
                timeout_seconds=timeout_seconds,
                kwargs=kwargs,
            )
        assert self._slots.acquire(blocking=False)
        try:
            return super().call_provider(
                specification,
                method,
                timeout_seconds=timeout_seconds,
                kwargs=kwargs,
            )
        finally:
            self._slots.release()


class _PostVisionRepairCapacityRouter(FailoverVisionProvider):
    """Make the selected channel unavailable only for the text repair call."""

    def __init__(self, channels):
        super().__init__(channels)
        self.repair_capacity_failures = 0

    def _execute_sticky(self, channel, method, *, boundary=None, **kwargs):
        held = False
        if method == "repair_json":
            held = channel.semaphore.acquire(blocking=False)
            assert held
            self.repair_capacity_failures += 1
        try:
            return super()._execute_sticky(channel, method, boundary=boundary, **kwargs)
        finally:
            if held:
                channel.semaphore.release()


def _start_boundary_server(mode: str):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BoundaryHTTPHandler)
    server.daemon_threads = True
    state = _BoundaryHTTPState(mode)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state


def _isolated_service(app, tmp_path, boundary):
    _, ids, prepared = prepare(app, tmp_path)
    settings = app.extensions["inktime_settings_repository"]
    settings.update(
        "analysis.execution_mode",
        "local_with_manual_ai",
        changed_by="test",
        source_ip="127.0.0.1",
    )
    service = PhotoAnalysisService(
        prepared.photos,
        prepared.usage,
        prepared.thumbnails,
        budgets=BudgetService(app.extensions["inktime_database"], settings),
        settings=settings,
        process_boundary=boundary,
        ai_traces=app.extensions["inktime_ai_trace_repository"],
    )
    return ids[0], service


def _isolated_provider(server, name: str = "boundary-provider"):
    return OpenAICompatibleProvider(
        name=name,
        base_url=f"http://127.0.0.1:{server.server_port}",
        api_key="",
        options={"allow_private_http": True},
        pricing={"test-model": {"input_per_million": 1.0, "output_per_million": 1.0}},
        timeout=5,
    )


def _test_job(app, name: str) -> str:
    return app.extensions["inktime_job_repository"].create_maintenance(
        kind="cleanup",
        name=name,
        settings={},
        created_by="test",
    )


def prepare(app, tmp_path, duplicate=False):
    root = tmp_path / "photos"
    root.mkdir()
    Image.new("RGB", (900, 600), (70, 120, 180)).save(root / "a.jpg")
    if duplicate:
        (root / "b.jpg").write_bytes((root / "a.jpg").read_bytes())
    photos = PhotoRepository(app.extensions["inktime_database"])
    cache = ThumbnailCache(tmp_path / "cache")
    result = PhotoScanner(photos, PhotoPreprocessor(), cache).scan("照片", root)
    with app.extensions["inktime_database"].session() as connection:
        ids = [row[0] for row in connection.execute("SELECT id FROM photos ORDER BY relative_path")]
    service = PhotoAnalysisService(photos, UsageRepository(app.extensions["inktime_database"]), cache)
    return result, ids, service


def test_single_model_call_returns_all_fields_and_usage(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    service.ai_traces = app.extensions["inktime_ai_trace_repository"]
    provider = MockProvider([valid_result()])
    result = service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=provider, strategy="high_quality", high_model="mock"
    )
    assert provider.analyze_calls == 1
    assert provider.repair_calls == 0
    thumbnail_path = Path(provider.analyze_kwargs[0]["image_path"])
    photo = service.photos.get_with_path(ids[0])
    original_path = Path(photo["root_path"]) / str(photo["relative_path"])
    assert thumbnail_path != original_path
    assert thumbnail_path.name.endswith("-1024.jpg")
    assert thumbnail_path.read_bytes() != original_path.read_bytes()
    assert provider.analyze_kwargs[0]["detail"] == "high"
    with Image.open(thumbnail_path) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert max(thumbnail.size) <= 1024
    assert result["analysis"]["side_caption"]
    with app.extensions["inktime_database"].session() as connection:
        usage = connection.execute("SELECT input_tokens,output_tokens FROM api_usage").fetchone()
        trace = connection.execute(
            "SELECT trace_id,status,final_result_json FROM ai_trace_runs WHERE photo_id=?", (ids[0],)
        ).fetchone()
        attempt = connection.execute(
            "SELECT attempt_kind,status,api_usage_id FROM ai_trace_attempts WHERE trace_id=?",
            (trace["trace_id"],),
        ).fetchone()
    assert tuple(usage) == (1000, 100)
    assert trace["status"] == "SUCCESS" and json.loads(trace["final_result_json"])["side_caption"]
    assert tuple(attempt[:2]) == ("vision", "SUCCESS") and attempt["api_usage_id"] is not None


def test_duplicate_model_types_are_normalized_without_paid_repair(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    service.ai_traces = app.extensions["inktime_ai_trace_repository"]
    duplicate_types = valid_result(types=["人物", "風景", "人物", "日常"])
    provider = MockProvider([duplicate_types])

    result = service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=provider, strategy="high_quality", high_model="mock"
    )

    assert result["analysis"]["types"] == ["人物", "風景", "日常"]
    assert provider.analyze_calls == 1
    assert provider.repair_calls == 0


def test_trace_persistence_failure_cannot_retry_provider_or_change_analysis(app, tmp_path):
    class RaisingTraceRepository:
        def __getattr__(self, _name):
            def raise_on_every_write(*_args, **_kwargs):
                raise RuntimeError("trace database unavailable")

            return raise_on_every_write

    _, ids, service = prepare(app, tmp_path)
    service.ai_traces = RaisingTraceRepository()
    provider = MockProvider([valid_result()])
    result = service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=provider, strategy="high_quality", high_model="mock"
    )
    assert provider.analyze_calls == 1
    assert provider.repair_calls == 0
    assert result["analysis"]["side_caption"]
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM photo_analysis WHERE photo_id=?", (ids[0],)
        ).fetchone()[0] == 1


def test_ambiguous_vision_failure_is_persisted_without_provider_failover(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    provider = AmbiguousProvider([])
    service.ai_traces = app.extensions["inktime_ai_trace_repository"]

    with pytest.raises(ProviderHTTPError):
        service.analyze_photo(
            photo_id=ids[0], job_id=None, provider=provider, strategy="high_quality", high_model="mock"
        )

    assert provider.analyze_calls == 1
    with app.extensions["inktime_database"].session() as connection:
        outcome = connection.execute(
            "SELECT outcome,requires_manual_confirmation,error_code FROM analysis_request_outcomes "
            "WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (ids[0],),
        ).fetchone()
        usage = connection.execute(
            "SELECT status,cost_source,error_code FROM api_usage WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (ids[0],),
        ).fetchone()
        trace = connection.execute(
            "SELECT status FROM ai_trace_runs WHERE photo_id=? ORDER BY id DESC LIMIT 1", (ids[0],)
        ).fetchone()
        attempt = connection.execute(
            "SELECT status,request_started_at FROM ai_trace_attempts WHERE trace_id=(SELECT trace_id FROM ai_trace_runs WHERE photo_id=? ORDER BY id DESC LIMIT 1)",
            (ids[0],),
        ).fetchone()
    assert tuple(outcome) == ("ambiguous_failed", 1, "VLM-AMBIGUOUS")
    assert tuple(usage) == ("failed", "unknown", "VLM-AMBIGUOUS")
    assert trace["status"] == "AMBIGUOUS"
    assert attempt["status"] == "AMBIGUOUS" and attempt["request_started_at"] is not None


def test_spawned_consumed_vision_timeout_is_terminal_and_billed_once(app, tmp_path):
    server, state = _start_boundary_server("vision_timeout")
    boundary = _ShortProviderBoundary()
    provider = _isolated_provider(server)
    photo_id, service = _isolated_service(app, tmp_path, boundary)
    job_id = _test_job(app, "consumed vision timeout")
    budgets = service.budgets
    try:
        with pytest.raises(TimeoutError) as raised:
            service.analyze_photo(
                photo_id=photo_id,
                job_id=job_id,
                provider=provider,
                strategy="high_quality",
                high_model="test-model",
                force_ai=True,
            )
        assert raised.value.code == "VLM-AMBIGUOUS"
        assert raised.value.ambiguous is True
        assert classify_failure(raised.value) == FailureClass.TERMINAL_NO_RETRY
        assert state.vision_requests == 1
        assert state.repair_requests == 0
        with app.extensions["inktime_database"].session() as connection:
            usage_rows = connection.execute(
                "SELECT status,cost_source,error_code,image_bytes,request_body_bytes "
                "FROM api_usage WHERE photo_id=? AND status='failed'",
                (photo_id,),
            ).fetchall()
            outcome = connection.execute(
                "SELECT outcome,requires_manual_confirmation,error_code "
                "FROM analysis_request_outcomes WHERE photo_id=? ORDER BY id DESC LIMIT 1",
                (photo_id,),
            ).fetchone()
            trace_attempt = connection.execute(
                "SELECT r.status,a.status,a.request_started_at,a.api_usage_id FROM ai_trace_runs r JOIN ai_trace_attempts a ON a.trace_id=r.trace_id WHERE r.photo_id=? ORDER BY r.id DESC,a.attempt_number DESC LIMIT 1",
                (photo_id,),
            ).fetchone()
        assert len(usage_rows) == 1
        assert tuple(usage_rows[0][:3]) == ("failed", "unknown", "VLM-AMBIGUOUS")
        assert usage_rows[0][3] > 0
        assert tuple(outcome) == ("ambiguous_failed", 1, "VLM-AMBIGUOUS")
        assert tuple(trace_attempt[:2]) == ("AMBIGUOUS", "AMBIGUOUS")
        assert trace_attempt["request_started_at"] is not None
        assert trace_attempt["api_usage_id"] is not None
        snapshot = budgets.snapshot(job_id=job_id, photo_id=photo_id)
        assert snapshot["photo_unknown_count"] == 1
        assert snapshot["job_unknown_count"] == 1
        with pytest.raises(BudgetExceeded):
            budgets.assert_request_allowed(job_id, photo_id)
    finally:
        boundary.shutdown()
        provider.close()
        state.release_blocked_response.set()
        server.shutdown()
        server.server_close()


def test_spawned_vision_5xx_is_terminal_once_without_failover_or_worker_retry(app, tmp_path):
    server, state = _start_boundary_server("vision_5xx")
    fallback_server, fallback_state = _start_boundary_server("normal")
    boundary = _ShortProviderBoundary()
    failing = _isolated_provider(server, "vision-5xx-provider")
    fallback = _isolated_provider(fallback_server, "vision-fallback-provider")
    router = FailoverVisionProvider(
        [
            ProviderChannel(failing, priority=1),
            ProviderChannel(fallback, priority=2),
        ]
    )
    photo_id, service = _isolated_service(app, tmp_path, boundary)
    job_id = _test_job(app, "consumed vision 5xx")
    try:
        with pytest.raises(ProcessCallError) as raised:
            service.analyze_photo(
                photo_id=photo_id,
                job_id=job_id,
                provider=router,
                strategy="high_quality",
                high_model="test-model",
                force_ai=True,
            )
        assert raised.value.code == "VLM-AMBIGUOUS"
        assert raised.value.ambiguous is True
        assert classify_failure(raised.value) == FailureClass.TERMINAL_NO_RETRY
        assert state.vision_requests == 1
        assert state.repair_requests == 0
        assert fallback_state.vision_requests == 0

        with app.extensions["inktime_database"].session() as connection:
            usage_rows = connection.execute(
                "SELECT status,cost_source,estimated_cost,actual_cost,error_code "
                "FROM api_usage WHERE photo_id=? AND status='failed'",
                (photo_id,),
            ).fetchall()
            outcome = connection.execute(
                "SELECT outcome,requires_manual_confirmation,error_code "
                "FROM analysis_request_outcomes WHERE photo_id=? ORDER BY id DESC LIMIT 1",
                (photo_id,),
            ).fetchone()
        assert len(usage_rows) == 1
        assert tuple(usage_rows[0]) == ("failed", "unknown", None, None, "VLM-AMBIGUOUS")
        assert tuple(outcome) == ("ambiguous_failed", 1, "VLM-AMBIGUOUS")
        assert service.budgets.snapshot(job_id=job_id, photo_id=photo_id)["photo_unknown_count"] == 1

        job_service = app.extensions["inktime_job_service"]
        job_repository = app.extensions["inktime_job_repository"]
        job_service.start(job_id)
        worker_calls = []

        def retrying_processor(_item):
            worker_calls.append(True)
            raise raised.value

        BoundedJobWorker(job_repository, retrying_processor, max_attempts=3).run_job(job_id)
        assert len(worker_calls) == 1
        assert job_repository.list_items(job_id)[0]["status"] == "failed"
        assert job_repository.list_items(job_id)[0]["attempts"] == 1
    finally:
        boundary.shutdown()
        failing.close()
        fallback.close()
        server.shutdown()
        server.server_close()
        fallback_server.shutdown()
        fallback_server.server_close()


def test_spawned_deterministic_vision_http_failure_does_not_create_unknown_budget(app, tmp_path):
    server, state = _start_boundary_server("vision_429")
    boundary = _ShortProviderBoundary()
    provider = _isolated_provider(server, "vision-rate-limit-provider")
    photo_id, service = _isolated_service(app, tmp_path, boundary)
    job_id = _test_job(app, "deterministic vision rate limit")
    try:
        with pytest.raises(ProcessCallError) as raised:
            service.analyze_photo(
                photo_id=photo_id,
                job_id=job_id,
                provider=provider,
                strategy="high_quality",
                high_model="test-model",
                force_ai=True,
            )
        assert raised.value.code == "VLM-002"
        assert raised.value.ambiguous is False
        assert classify_failure(raised.value) == FailureClass.RETRYABLE
        assert state.vision_requests == 1
        assert state.repair_requests == 0
        with app.extensions["inktime_database"].session() as connection:
            failed_count = connection.execute(
                "SELECT COUNT(*) FROM api_usage WHERE photo_id=? AND status='failed'",
                (photo_id,),
            ).fetchone()[0]
        assert failed_count == 0
        snapshot = service.budgets.snapshot(job_id=job_id, photo_id=photo_id)
        assert snapshot["photo_unknown_count"] == 0
        assert snapshot["job_unknown_count"] == 0
    finally:
        boundary.shutdown()
        provider.close()
        server.shutdown()
        server.server_close()


def test_spawned_vision_capacity_timeout_is_pre_execution_and_retryable(app, tmp_path):
    server, state = _start_boundary_server("vision_timeout")
    boundary = _ShortProviderBoundary(timeout_seconds=0.2)
    provider = _isolated_provider(server, "capacity-provider")
    photo_id, service = _isolated_service(app, tmp_path, boundary)
    job_id = _test_job(app, "vision capacity timeout")
    assert boundary._slots.acquire(timeout=1.0)
    try:
        with pytest.raises(TimeoutError) as raised:
            service.analyze_photo(
                photo_id=photo_id,
                job_id=job_id,
                provider=provider,
                strategy="high_quality",
                high_model="test-model",
                force_ai=True,
            )
        assert raised.value.code == "AI-PROVIDER-TIMEOUT"
        assert classify_failure(raised.value) == FailureClass.RETRYABLE
        assert state.vision_requests == 0
        assert state.repair_requests == 0
        with app.extensions["inktime_database"].session() as connection:
            failed_count = connection.execute(
                "SELECT COUNT(*) FROM api_usage WHERE photo_id=? AND status='failed'",
                (photo_id,),
            ).fetchone()[0]
            trace_attempt = connection.execute(
                "SELECT r.status,a.request_started_at FROM ai_trace_runs r JOIN ai_trace_attempts a ON a.trace_id=r.trace_id WHERE r.photo_id=? ORDER BY r.id DESC LIMIT 1",
                (photo_id,),
            ).fetchone()
        assert failed_count == 0
        assert tuple(trace_attempt) == ("TIMEOUT", None)
    finally:
        boundary._slots.release()
        boundary.shutdown()
        provider.close()
        server.shutdown()
        server.server_close()


def test_spawned_consumed_repair_timeout_records_repair_unknown_without_second_vision(
    app, tmp_path
):
    server, state = _start_boundary_server("invalid_then_repair_timeout")
    boundary = _ShortProviderBoundary()
    provider = _isolated_provider(server, "repair-timeout-provider")
    photo_id, service = _isolated_service(app, tmp_path, boundary)
    job_id = _test_job(app, "consumed repair timeout")
    try:
        with pytest.raises(TimeoutError) as raised:
            service.analyze_photo(
                photo_id=photo_id,
                job_id=job_id,
                provider=provider,
                strategy="high_quality",
                high_model="test-model",
                force_ai=True,
            )
        assert raised.value.code == "VLM-AMBIGUOUS"
        assert raised.value.ambiguous is True
        assert classify_failure(raised.value) == FailureClass.TERMINAL_NO_RETRY
        assert state.vision_requests == 1
        assert state.repair_requests == 1
        with app.extensions["inktime_database"].session() as connection:
            usage_rows = connection.execute(
                "SELECT request_type,status,cost_source,error_code,image_bytes,request_body_bytes "
                "FROM api_usage WHERE photo_id=? AND status='failed'",
                (photo_id,),
            ).fetchall()
            outcome = connection.execute(
                "SELECT outcome,requires_manual_confirmation,error_code "
                "FROM analysis_request_outcomes WHERE photo_id=? ORDER BY id DESC LIMIT 1",
                (photo_id,),
            ).fetchone()
            trace_attempts = connection.execute(
                "SELECT attempt_kind,status,request_started_at,api_usage_id FROM ai_trace_attempts WHERE trace_id=(SELECT trace_id FROM ai_trace_runs WHERE photo_id=? ORDER BY id DESC LIMIT 1) ORDER BY attempt_number",
                (photo_id,),
            ).fetchall()
        assert len(usage_rows) == 1
        assert tuple(usage_rows[0][:4]) == (
            "json_repair",
            "failed",
            "unknown",
            "VLM-AMBIGUOUS",
        )
        assert usage_rows[0][4] == 0
        assert usage_rows[0][5] > 0
        assert tuple(outcome) == ("ambiguous_failed", 1, "VLM-AMBIGUOUS")
        assert [row["attempt_kind"] for row in trace_attempts] == ["vision", "json_repair"]
        assert trace_attempts[1]["status"] == "AMBIGUOUS"
        assert trace_attempts[1]["request_started_at"] is not None
        assert trace_attempts[1]["api_usage_id"] is not None
        snapshot = service.budgets.snapshot(job_id=job_id, photo_id=photo_id)
        assert snapshot["photo_unknown_count"] == 1
        assert snapshot["job_unknown_count"] == 1
    finally:
        boundary.shutdown()
        provider.close()
        state.release_blocked_response.set()
        server.shutdown()
        server.server_close()


def test_repair_capacity_timeout_after_vision_is_terminal_without_repair_unknown_or_second_vision(
    app, tmp_path
):
    server, state = _start_boundary_server("invalid_then_repair_capacity")
    boundary = _RepairCapacityBoundary()
    provider = _isolated_provider(server, "repair-capacity-provider")
    photo_id, service = _isolated_service(app, tmp_path, boundary)
    job_id = _test_job(app, "repair capacity timeout")
    try:
        with pytest.raises(TimeoutError) as raised:
            service.analyze_photo(
                photo_id=photo_id,
                job_id=job_id,
                provider=provider,
                strategy="high_quality",
                high_model="test-model",
                force_ai=True,
            )
        assert raised.value.code == "VLM-004"
        assert classify_failure(raised.value) == FailureClass.TERMINAL_NO_RETRY
        assert state.vision_requests == 1
        assert state.repair_requests == 0
        with app.extensions["inktime_database"].session() as connection:
            failed_count = connection.execute(
                "SELECT COUNT(*) FROM api_usage WHERE photo_id=? AND status='failed'",
                (photo_id,),
            ).fetchone()[0]
            repair_attempt = connection.execute(
                "SELECT status,request_started_at,api_usage_id FROM ai_trace_attempts WHERE trace_id=(SELECT trace_id FROM ai_trace_runs WHERE photo_id=? ORDER BY id DESC LIMIT 1) AND attempt_kind='json_repair'",
                (photo_id,),
            ).fetchone()
        assert failed_count == 0
        assert tuple(repair_attempt) == ("TIMEOUT", None, None)
    finally:
        boundary.shutdown()
        provider.close()
        server.shutdown()
        server.server_close()


def test_router_repair_capacity_is_terminal_to_worker_without_second_vision(app, tmp_path):
    boundary = KillableProcessBoundary(max_processes=1, terminate_grace_seconds=0.05)
    provider = MockProvider(["not-json"])
    router = _PostVisionRepairCapacityRouter([ProviderChannel(provider, max_concurrency=1)])
    photo_id, service = _isolated_service(app, tmp_path, boundary)
    jobs = app.extensions["inktime_job_service"]
    repository = app.extensions["inktime_job_repository"]
    job_id = jobs.create_analysis_job(
        name="router repair capacity",
        strategy="high_quality",
        settings={},
        created_by="test",
        budget_limit=None,
        photo_ids=[photo_id],
    )
    jobs.start(job_id)
    processor_calls = 0
    errors = []

    def process(item):
        nonlocal processor_calls
        processor_calls += 1
        try:
            return service.analyze_photo(
                photo_id=item["photo_id"],
                job_id=item["job_id"],
                provider=router,
                strategy="high_quality",
                high_model="mock",
                force_ai=True,
            )
        except Exception as error:
            errors.append(error)
            raise

    try:
        BoundedJobWorker(repository, process, max_attempts=3).run_job(job_id)
    finally:
        boundary.shutdown()

    assert processor_calls == 1
    assert provider.analyze_calls == 1
    assert provider.repair_calls == 0
    assert router.repair_capacity_failures == 1
    assert len(errors) == 1
    assert errors[0].code == "VLM-004"
    assert classify_failure(errors[0]) == FailureClass.TERMINAL_NO_RETRY
    item = repository.list_items(job_id)[0]
    assert item["status"] == "failed"
    assert item["error_code"] == "VLM-004"
    assert item["attempts"] == 1
    assert repository.get(job_id)["status"] == "completed_with_errors"


@pytest.mark.parametrize(
    "mode",
    [
        "invalid_then_repair_429",
        "invalid_then_repair_redirect",
        "invalid_then_repair_5xx",
    ],
)
def test_router_worker_repair_http_errors_are_terminal_without_second_vision(
    app, tmp_path, mode
):
    server, state = _start_boundary_server(mode)
    boundary = _ShortProviderBoundary()
    provider = _isolated_provider(server, f"{mode}-provider")
    router = FailoverVisionProvider([ProviderChannel(provider, max_concurrency=1)])
    photo_id, service = _isolated_service(app, tmp_path, boundary)
    jobs = app.extensions["inktime_job_service"]
    repository = app.extensions["inktime_job_repository"]
    job_id = jobs.create_analysis_job(
        name=f"{mode} router repair",
        strategy="high_quality",
        settings={},
        created_by="test",
        budget_limit=None,
        photo_ids=[photo_id],
    )
    jobs.start(job_id)
    processor_calls = 0
    errors = []

    def process(item):
        nonlocal processor_calls
        processor_calls += 1
        try:
            return service.analyze_photo(
                photo_id=item["photo_id"],
                job_id=item["job_id"],
                provider=router,
                strategy="high_quality",
                high_model="test-model",
                force_ai=True,
            )
        except Exception as error:
            errors.append(error)
            raise

    try:
        BoundedJobWorker(repository, process, max_attempts=3).run_job(job_id)
    finally:
        boundary.shutdown()
        provider.close()
        server.shutdown()
        server.server_close()

    assert processor_calls == 1
    assert len(errors) == 1
    assert errors[0].code == "VLM-004"
    assert classify_failure(errors[0]) == FailureClass.TERMINAL_NO_RETRY
    assert state.vision_requests == 1
    assert state.repair_requests == 1
    with app.extensions["inktime_database"].session() as connection:
        usage = connection.execute(
            "SELECT request_type,status,cost_source,error_code FROM api_usage "
            "WHERE photo_id=? AND status='failed'",
            (photo_id,),
        ).fetchall()
        outcome = connection.execute(
            "SELECT outcome,requires_manual_confirmation,error_code "
            "FROM analysis_request_outcomes WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (photo_id,),
        ).fetchone()
    assert len(usage) == 0
    assert tuple(outcome) == ("failed", 0, "VLM-004")
    item = repository.list_items(job_id)[0]
    assert item["status"] == "failed"
    assert item["error_code"] == "VLM-004"
    assert item["attempts"] == 1
    assert repository.get(job_id)["status"] == "completed_with_errors"


def test_provider_and_local_results_persist_a_complete_analysis_context(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=MockProvider([valid_result()]), strategy="high_quality"
    )
    (tmp_path / "local").mkdir()
    _, local_ids, local_service = prepare(app, tmp_path / "local")
    local_service.analyze_photo(photo_id=local_ids[0], job_id=None, provider=None, strategy="local")
    with app.extensions["inktime_database"].session() as connection:
        rows = connection.execute(
            "SELECT analysis_fingerprint,analysis_spec_json,prompt_version,schema_kind,"
            "scoring_version_id,vision_request_fingerprint,vision_input_spec_json "
            "FROM photo_analysis ORDER BY created_at,id"
        ).fetchall()
    assert len(rows) == 2
    assert all(row["analysis_fingerprint"] and row["analysis_spec_json"] for row in rows)
    assert all(row["prompt_version"] and row["schema_kind"] for row in rows)
    assert all(row["vision_request_fingerprint"] and row["vision_input_spec_json"] for row in rows)


def test_favorite_change_recalculates_latest_ranking_with_original_version(app, tmp_path):
    user_id = create_admin(app)
    _, ids, service = prepare(app, tmp_path)
    profile = app.extensions["inktime_scoring_repository"].current()
    provider = MockProvider([valid_result()])
    service.analyze_photo(
        photo_id=ids[0],
        job_id=None,
        provider=provider,
        strategy="high_quality",
        high_model="mock",
        scoring_version_id=str(profile["id"]),
    )
    repository = app.extensions["inktime_photo_repository"]
    with app.extensions["inktime_database"].session() as connection:
        before = connection.execute(
            "SELECT ranking_score,scoring_version_id FROM photo_analysis WHERE photo_id=?",
            (ids[0],),
        ).fetchone()

    repository.update_manual(
        ids[0],
        favorite=True,
        captured_at=None,
        types=["人物"],
        side_caption="值得收藏的一天",
        changed_by=user_id,
    )

    with app.extensions["inktime_database"].session() as connection:
        after = connection.execute(
            "SELECT ranking_score,scoring_version_id FROM photo_analysis WHERE photo_id=?",
            (ids[0],),
        ).fetchone()
    assert after["ranking_score"] == before["ranking_score"] + profile["favorite_bonus"]
    assert after["scoring_version_id"] == before["scoring_version_id"]


def test_invalid_json_is_repaired_only_once_without_second_image_call(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    service.ai_traces = app.extensions["inktime_ai_trace_repository"]
    provider = MockProvider(["not-json", valid_result()])
    service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=provider, strategy="high_quality", high_model="mock"
    )
    assert provider.analyze_calls == 1
    assert provider.repair_calls == 1
    assert provider.repair_kwargs[0]["max_tokens"] == 1200
    assert "image_path" not in provider.repair_kwargs[0]
    with app.extensions["inktime_database"].session() as connection:
        attempts = connection.execute(
            "SELECT attempt_kind,status FROM ai_trace_attempts ORDER BY attempt_number"
        ).fetchall()
    assert [tuple(row) for row in attempts] == [
        ("vision", "VALIDATION_FAILED"),
        ("json_repair", "SUCCESS"),
    ]


def test_invalid_repair_container_fails_without_a_second_vision_request(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    provider = MockProvider(["[]", "{}"])
    with pytest.raises(AnalysisValidationError):
        service.analyze_photo(
            photo_id=ids[0], job_id=None, provider=provider, strategy="high_quality", high_model="mock"
        )
    assert provider.analyze_calls == 1
    assert provider.repair_calls == 1
    assert provider.repair_kwargs[0]["max_tokens"] == 1200
    assert "image_path" not in provider.repair_kwargs[0]


def test_router_does_not_fail_over_after_initial_vision_and_repair_failure(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    first = MockProvider(["[]", "{}"])
    first.provider_id = "first-vision"
    second = MockProvider([valid_result()])
    second.provider_id = "second-vision"
    router = FailoverVisionProvider(
        [ProviderChannel(first, priority=1), ProviderChannel(second, priority=2)],
        failure_threshold=1,
    )

    with pytest.raises(AnalysisValidationError):
        service.analyze_photo(
            photo_id=ids[0], job_id=None, provider=router, strategy="high_quality", high_model="mock"
        )

    assert first.analyze_calls == 1
    assert first.repair_calls == 1
    assert second.analyze_calls == 0


def test_full_analysis_hits_historical_v2_cache_without_an_image_call(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    # This fixture intentionally models a pre-contract frozen plan.  New
    # plans include the v3 provider contract and must not fall back to v2.
    legacy_plan = build_analysis_plan(
        strategy="high_quality",
        provider_route=[],
        low_model="mock",
        high_model="mock",
        stage_two_threshold=65,
        favorite_override=True,
        scoring_profile={
            "id": "",
            "memory_weight": 25,
            "beauty_weight": 25,
            "technical_weight": 25,
            "emotion_weight": 25,
            "favorite_bonus": 0,
        },
        caption_controls=None,
        prompt_version="legacy-test-prompt",
        high_image_max_side=1024,
        repair_policy={"model": "mock", "max_tokens": 1200},
    )
    for key in (
        "scoring_rules",
        "scoring_rules_sha256",
        "provider_behavior_revision",
        "provider_prompt_contract_sha256",
    ):
        legacy_plan.pop(key, None)
    first = MockProvider([valid_result()])
    service.analyze_photo(
        photo_id=ids[0],
        job_id=None,
        provider=first,
        strategy="high_quality",
        analysis_plan=legacy_plan,
        force_ai=True,
    )
    repository = app.extensions["inktime_photo_repository"]
    with app.extensions["inktime_database"].session() as connection:
        analysis = connection.execute(
            "SELECT p.sha256 AS content_sha256,a.provider,a.model,a.prompt_version,a.schema_kind,"
            "a.analysis_spec_json,a.vision_input_spec_json "
            "FROM photo_analysis a JOIN photos p ON p.id=a.photo_id "
            "WHERE a.photo_id=? ORDER BY a.id DESC LIMIT 1",
            (ids[0],),
        ).fetchone()
        cached = connection.execute(
            "SELECT result_json,raw_json,input_tokens,output_tokens,cached_tokens,estimated_cost,latency_ms "
            "FROM ai_analysis_cache WHERE content_sha256=? ORDER BY created_at DESC LIMIT 1",
            (analysis["content_sha256"],),
        ).fetchone()
    assert cached is not None
    analysis_spec = json.loads(analysis["analysis_spec_json"])
    vision_input = json.loads(analysis["vision_input_spec_json"])
    legacy_fingerprint = fingerprint(
        {
            "content_sha256": analysis["content_sha256"],
            "actual_provider": analysis["provider"],
            "model": analysis["model"],
            "prompt_version": analysis["prompt_version"],
            "schema_kind": analysis["schema_kind"],
            "reasoning_effort": analysis_spec["reasoning_effort"],
            **vision_input,
            "schema_version": 2,
        }
    )
    repository.put_ai_cache(
        content_sha256=analysis["content_sha256"],
        provider=analysis["provider"],
        model_name=analysis["model"],
        prompt_version=analysis["prompt_version"],
        schema_version=2,
        schema_kind=analysis["schema_kind"],
        result=json.loads(cached["result_json"]),
        raw_json=cached["raw_json"],
        input_tokens=cached["input_tokens"],
        output_tokens=cached["output_tokens"],
        cached_tokens=cached["cached_tokens"],
        estimated_cost=cached["estimated_cost"],
        latency_ms=cached["latency_ms"],
        vision_request_fingerprint=legacy_fingerprint,
        vision_input_spec_json=analysis["vision_input_spec_json"],
    )

    second = MockProvider([])
    result = service.analyze_photo(
        photo_id=ids[0],
        job_id=None,
        provider=second,
        strategy="high_quality",
        analysis_plan=legacy_plan,
        force_ai=True,
    )
    assert result["stage"] == "cache"
    assert second.analyze_calls == 0


def test_legacy_smart_strategy_uses_the_single_full_contract(app, tmp_path):
    _, ids, service = prepare(app, tmp_path)
    provider = MockProvider([valid_result(memory_score=40, types=["雜物"])])
    result = service.analyze_photo(
        photo_id=ids[0],
        job_id=None,
        provider=provider,
        strategy="smart_two_stage",
        low_model="cheap",
        high_model="quality",
    )
    assert result["stage"] == "single"
    assert provider.analyze_calls == 1


def test_failover_rebuilds_cache_identity_for_the_next_provider(app, tmp_path):
    _, ids, _service = prepare(app, tmp_path)
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.ai_mode", "eligible", changed_by="test", source_ip="127.0.0.1")
    settings.update("analysis.prefilter_enabled", False, changed_by="test", source_ip="127.0.0.1")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET eligible=1,exclusion_status='eligible' WHERE id=?", (ids[0],))
    failing = FailingProvider([])
    failing.provider_id = "first-provider"
    succeeding = MockProvider([valid_result()])
    succeeding.provider_id = "second-provider"
    router = FailoverVisionProvider(
        [ProviderChannel(failing, priority=1), ProviderChannel(succeeding, priority=2)],
        failure_threshold=1,
    )

    analysis = app.extensions["inktime_analysis_service"]
    plan = analysis.build_plan(
        strategy="high_quality",
        provider_route=[],
        scoring_profile=dict(app.extensions["inktime_scoring_repository"].current()),
    )
    result = analysis.analyze_photo(
        photo_id=ids[0], job_id=None, provider=router, strategy="high_quality", analysis_plan=plan
    )

    assert result["stage"] == "single"
    assert failing.analyze_calls == 1
    assert succeeding.analyze_calls == 1
    with app.extensions["inktime_database"].session() as connection:
        cache = connection.execute("SELECT provider FROM ai_analysis_cache").fetchone()
    assert cache["provider"] == "second-provider"


def test_new_analysis_plan_forces_single_literary_caption_and_no_reasoning(app):
    settings = app.extensions["inktime_settings_repository"]
    settings.update(
        "analysis.caption_variants_enabled",
        True,
        changed_by="legacy-operator",
        source_ip="127.0.0.1",
    )
    settings.update(
        "batch.reasoning_effort",
        "high",
        changed_by="legacy-operator",
        source_ip="127.0.0.1",
    )
    service = app.extensions["inktime_analysis_service"]
    plan = service.build_plan(
        strategy="high_quality",
        provider_route=[],
        scoring_profile=dict(app.extensions["inktime_scoring_repository"].current()),
    )
    assert plan["caption_controls"]["caption_variants_enabled"] is False
    assert plan["caption_controls"]["copy_default_style"] == "literary"
    assert plan["reasoning_effort"] == "none"


def test_identical_photo_inherits_without_model_call(app, tmp_path):
    scan, ids, service = prepare(app, tmp_path, duplicate=True)
    assert scan["inherited"] == 1
    assert len(ids) == 2
    first = MockProvider([valid_result()])
    service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=first, strategy="high_quality", high_model="mock"
    )
    second = MockProvider([])
    result = service.analyze_photo(
        photo_id=ids[1], job_id=None, provider=second, strategy="high_quality", high_model="mock"
    )
    assert result["stage"] == "inherited"
    assert second.analyze_calls == 0


def test_worker_context_inherits_only_the_same_frozen_plan_and_keeps_source_trace(app, tmp_path):
    _, ids, _service = prepare(app, tmp_path, duplicate=True)
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.ai_mode", "eligible", changed_by="test", source_ip="127.0.0.1")
    settings.update("analysis.prefilter_enabled", False, changed_by="test", source_ip="127.0.0.1")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id IN (?,?)",
            ids,
        )
    service = app.extensions["inktime_analysis_service"]
    plan = service.build_plan(
        strategy="high_quality",
        provider_route=[],
        scoring_profile=dict(app.extensions["inktime_scoring_repository"].current()),
    )
    first = MockProvider([valid_result()])
    service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=first, strategy="high_quality", analysis_plan=plan
    )
    second = MockProvider([])
    inherited = service.analyze_photo(
        photo_id=ids[1], job_id=None, provider=second, strategy="high_quality", analysis_plan=plan
    )
    assert inherited["stage"] == "inherited"
    assert first.analyze_calls == 1
    assert second.analyze_calls == 0
    with app.extensions["inktime_database"].session() as connection:
        source = connection.execute(
            "SELECT id,analysis_fingerprint,vision_request_fingerprint,vision_input_spec_json FROM photo_analysis WHERE photo_id=?",
            (ids[0],),
        ).fetchone()
        copied = connection.execute(
            "SELECT stage,analysis_fingerprint,vision_request_fingerprint,vision_input_spec_json,semantic_json "
            "FROM photo_analysis WHERE photo_id=?",
            (ids[1],),
        ).fetchone()
    assert copied["stage"] == "inherited"
    identity_plan = dict(plan)
    identity_plan.pop("caption_display_controls", None)
    identity_plan.pop("repair_policy", None)
    assert copied["analysis_fingerprint"] == fingerprint(identity_plan) == source["analysis_fingerprint"]
    assert copied["vision_request_fingerprint"] == source["vision_request_fingerprint"]
    assert copied["vision_input_spec_json"] == source["vision_input_spec_json"]
    assert json.loads(copied["semantic_json"])["inherited_from"]["analysis_id"] == source["id"]


def test_worker_context_does_not_inherit_a_different_frozen_plan(app, tmp_path):
    _, ids, _service = prepare(app, tmp_path, duplicate=True)
    settings = app.extensions["inktime_settings_repository"]
    settings.update("analysis.ai_mode", "eligible", changed_by="test", source_ip="127.0.0.1")
    settings.update("analysis.prefilter_enabled", False, changed_by="test", source_ip="127.0.0.1")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id IN (?,?)",
            ids,
        )
    service = app.extensions["inktime_analysis_service"]
    profile = dict(app.extensions["inktime_scoring_repository"].current())
    first_plan = service.build_plan(strategy="high_quality", provider_route=[], scoring_profile=profile)
    first = MockProvider([valid_result()])
    service.analyze_photo(
        photo_id=ids[0], job_id=None, provider=first, strategy="high_quality", analysis_plan=first_plan
    )
    settings.update("model.analysis_model", "new-model", changed_by="test", source_ip="127.0.0.1")
    second_plan = service.build_plan(strategy="high_quality", provider_route=[], scoring_profile=profile)
    second = MockProvider([valid_result(memory_score=77)])
    result = service.analyze_photo(
        photo_id=ids[1], job_id=None, provider=second, strategy="high_quality", analysis_plan=second_plan
    )
    assert result["stage"] == "single"
    assert second.analyze_calls == 1


def test_cloud_strategy_prefilters_screenshot_without_token_usage(app, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    Image.new("RGB", (900, 600), "white").save(root / "螢幕快照.png")
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(
        photos,
        PhotoPreprocessor(),
        app.extensions["inktime_thumbnail_cache"],
    ).scan("截圖", root, build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
    provider = MockProvider([])
    photo = photos.get_with_path(photo_id)
    snapshot = app.extensions["inktime_analysis_service"].prefilter_snapshot(photo)

    result = app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=provider,
        strategy="smart_two_stage",
    )

    assert result["stage"] == "prefilter"
    assert result["analysis"]["should_keep"] is False
    assert snapshot["decision"] == "auto_excluded"
    assert snapshot["primary_reason"] == "screenshot"
    assert any(check["key"] == "screenshot_strong" and check["hit"] for check in snapshot["checks"])
    assert provider.analyze_calls == 0
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0


def test_prefilter_persists_photo_analysis_and_audit_in_one_transaction(app, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    Image.new("RGB", (900, 600), "white").save(root / "螢幕快照.png")
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan(
        "截圖", root, build_thumbnails=False
    )
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id=?",
            (photo_id,),
        )
    result = app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id, job_id=None, provider=MockProvider([]), strategy="high_quality"
    )
    assert result["stage"] == "prefilter"
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT eligible,exclusion_status,reject_reason FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        analysis = connection.execute(
            "SELECT stage,analysis_fingerprint FROM photo_analysis WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (photo_id,),
        ).fetchone()
        event = connection.execute(
            "SELECT event FROM photo_events WHERE photo_id=? ORDER BY id DESC LIMIT 1", (photo_id,)
        ).fetchone()
    assert tuple(photo) == (0, "auto_excluded", "screenshot")
    assert analysis["stage"] == "prefilter"
    assert analysis["analysis_fingerprint"]
    assert event["event"] == "automatic_exclusion"


def test_prefilter_does_not_overwrite_a_manual_restore(app, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    Image.new("RGB", (900, 600), "white").save(root / "螢幕快照.png")
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan(
        "截圖", root, build_thumbnails=False
    )
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='manually_restored',manual_override=1 WHERE id=?",
            (photo_id,),
        )
    photos.save_analysis(
        photo_id,
        None,
        "prefilter",
        "local",
        "local-prefilter",
        valid_result(),
        "{}",
        prefilter_evaluation={
            "decision": "auto_excluded",
            "primary_reason": "screenshot",
            "feature_version": "local-quality-v5",
        },
    )
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT eligible,exclusion_status FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        event = connection.execute(
            "SELECT event FROM photo_events WHERE photo_id=? ORDER BY id DESC LIMIT 1", (photo_id,)
        ).fetchone()
    assert tuple(photo) == (1, "manually_restored")
    assert event["event"] == "automatic_exclusion_skipped"


def test_prefilter_transaction_rolls_back_photo_and_audit_when_analysis_insert_fails(app, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    Image.new("RGB", (900, 600), "white").save(root / "螢幕快照.png")
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(photos, PhotoPreprocessor(), app.extensions["inktime_thumbnail_cache"]).scan(
        "截圖", root, build_thumbnails=False
    )
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
        connection.execute(
            "UPDATE photos SET eligible=1,exclusion_status='eligible',manual_override=0 WHERE id=?",
            (photo_id,),
        )
        before = tuple(
            connection.execute(
                "SELECT eligible,exclusion_status,reject_reason FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
        )
        connection.execute(
            "CREATE TRIGGER fail_prefilter_analysis BEFORE INSERT ON photo_analysis "
            f"WHEN NEW.photo_id='{photo_id}' BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="forced failure"):
        app.extensions["inktime_analysis_service"].analyze_photo(
            photo_id=photo_id, job_id=None, provider=MockProvider([]), strategy="high_quality"
        )
    with app.extensions["inktime_database"].session() as connection:
        photo = connection.execute(
            "SELECT eligible,exclusion_status,reject_reason FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        events = connection.execute(
            "SELECT COUNT(*) FROM photo_events WHERE photo_id=?", (photo_id,)
        ).fetchone()[0]
    assert tuple(photo) == before
    assert events == 0


def test_prefilter_snapshot_requires_two_quality_defects(app, tmp_path):
    root = tmp_path / "quality"
    root.mkdir()
    Image.new("RGB", (900, 600), "gray").save(root / "plain.jpg")
    photos = app.extensions["inktime_photo_repository"]
    PhotoScanner(
        photos,
        PhotoPreprocessor(),
        app.extensions["inktime_thumbnail_cache"],
    ).scan("品質", root, build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])

    snapshot = app.extensions["inktime_analysis_service"].prefilter_snapshot(photos.get_with_path(photo_id))

    assert snapshot["decision"] == "auto_excluded"
    assert snapshot["primary_reason"] == "severe_blur"
    assert "severe_blur" in snapshot["matched_checks"]
