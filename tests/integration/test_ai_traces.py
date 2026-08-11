from __future__ import annotations

from datetime import datetime, timezone
import json

from PIL import Image

from inktime.app.domain.photos import PhotoPreprocessor, ThumbnailCache
from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.repositories.ai_traces import AITraceRepository
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.analysis import PhotoAnalysisService
from inktime.app.workers.scanner import PhotoScanner
from tests.conftest import create_admin, login
from tests.unit.test_analysis_schema import valid_result


class TraceProvider(VisionProvider):
    name = "Trace Provider"
    provider_id = "trace-provider"

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.analyze_calls = 0
        self.repair_calls = 0

    @staticmethod
    def _response(value: dict, *, repair: bool = False) -> ProviderResponse:
        now = datetime.now(timezone.utc).isoformat()
        content = json.dumps(value, ensure_ascii=False)
        request = {
            "model": "trace-model",
            "messages": [
                {"role": "system", "content": "system trace prompt"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "user trace prompt"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64," + "A" * 4096,
                                "detail": "high",
                            },
                        },
                    ],
                },
            ],
            "Authorization": "Bearer must-never-persist",
            "max_tokens": 1200 if repair else 2048,
            "temperature": 0.1,
            "response_format": {"type": "json_schema", "json_schema": {"name": "photo"}},
        }
        return ProviderResponse(
            content,
            Usage(100, 20, 5, 2),
            "request-safe-1",
            {},
            "/chat/completions",
            200,
            request,
            json.dumps({"choices": [{"message": {"content": content}}]}),
            now,
            now,
        )

    def analyze(self, **_kwargs) -> ProviderResponse:
        self.analyze_calls += 1
        return self._response(self.responses.pop(0))

    def repair_json(self, **_kwargs) -> ProviderResponse:
        self.repair_calls += 1
        return self._response(self.responses.pop(0), repair=True)

    def submit_batch(self, requests, completion_window="24h"):
        raise NotImplementedError

    def poll_batch(self, batch_id):
        raise NotImplementedError

    def cancel_batch(self, batch_id):
        raise NotImplementedError

    def estimate_cost(self, model, usage):
        return (usage.input_tokens + usage.output_tokens) / 1_000_000

    def validate_config(self):
        return True, "ok"


class FailedTraceProvider(TraceProvider):
    def analyze(self, **_kwargs) -> ProviderResponse:
        self.analyze_calls += 1
        error = ProviderHTTPError(
            "rate limited",
            "VLM-002",
            http_status=429,
            provider_error_code="rate_limit",
            vision_started=True,
        )
        error.trace_request_json = {
            "Authorization": "Bearer must-never-persist",
            "messages": [{"role": "user", "content": "retry later"}],
        }
        error.trace_endpoint = "/chat/completions"
        error.trace_http_status = 429
        error.trace_request_built_at = datetime.now(timezone.utc).isoformat()
        raise error


def _analysis_fixture(app, tmp_path):
    root = tmp_path / "trace-photos"
    root.mkdir()
    Image.new("RGB", (900, 600), (70, 120, 180)).save(root / "trace.jpg")
    photos = PhotoRepository(app.extensions["inktime_database"])
    thumbnails = ThumbnailCache(tmp_path / "trace-cache")
    PhotoScanner(photos, PhotoPreprocessor(), thumbnails).scan("Trace 照片", root)
    with app.extensions["inktime_database"].session() as connection:
        photo_id = str(connection.execute("SELECT id FROM photos").fetchone()[0])
    traces = AITraceRepository(app.extensions["inktime_database"])
    service = PhotoAnalysisService(
        photos,
        UsageRepository(app.extensions["inktime_database"]),
        thumbnails,
        traces=traces,
    )
    return photo_id, traces, service


def test_trace_routes_require_auth_and_navigation_has_clickable_entry(client, app) -> None:
    assert client.get("/ai/traces").status_code in {302, 401}
    create_admin(app)
    assert client.get("/api/v1/ai/traces").status_code == 401
    login(client)
    page = client.get("/ai/traces")
    assert page.status_code == 200
    assert 'href="/ai/traces"' in page.get_data(as_text=True)
    assert "AI 分析" in page.get_data(as_text=True)


def test_successful_provider_call_persists_bounded_sanitized_trace_and_final_result(
    client, app, tmp_path
) -> None:
    create_admin(app)
    login(client)
    photo_id, _traces, service = _analysis_fixture(app, tmp_path)
    provider = TraceProvider([valid_result()])
    result = service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=provider,
        strategy="high_quality",
        high_model="trace-model",
    )
    assert result["analysis"]["side_caption"]
    assert provider.analyze_calls == 1

    listing = client.get("/api/v1/ai/traces?limit=1000&photo_id=" + photo_id)
    assert listing.status_code == 200
    assert len(listing.json["traces"]) == 1
    summary = listing.json["traces"][0]
    assert summary["photo_id"] == photo_id
    assert summary["status"] == "SUCCESS"
    assert summary["input_tokens"] == 100
    assert summary["retry_count"] == 0

    detail = client.get("/api/v1/ai/traces/" + summary["trace_id"]).json["trace"]
    assert detail["photo_url"] == "/photos/" + photo_id
    assert detail["final_result"]["side_caption"] == result["analysis"]["side_caption"]
    attempt = detail["attempts"][0]
    persisted = json.dumps(attempt["request"], ensure_ascii=False)
    assert attempt["system_prompt"] == "system trace prompt"
    assert attempt["user_prompt"] == "user trace prompt"
    assert attempt["response_raw"]
    assert attempt["response_parsed"]["side_caption"]
    assert attempt["cost_source"] == "estimated"
    assert "must-never-persist" not in persisted
    assert "data:image" not in persisted
    assert "[IMAGE_PAYLOAD_REDACTED]" in persisted

    photo_page = client.get("/photos/" + photo_id).get_data(as_text=True)
    assert "/ai/traces?photo_id=" + photo_id in photo_page

    app.extensions["inktime_auth_repository"].create_user(
        "trace-viewer", "trace-viewer-passphrase", role="viewer"
    )
    viewer = app.test_client()
    login(viewer, "trace-viewer", "trace-viewer-passphrase")
    viewer_attempt = viewer.get("/api/v1/ai/traces/" + summary["trace_id"]).json["trace"][
        "attempts"
    ][0]
    assert viewer_attempt["request"] is None
    assert viewer_attempt["response_raw"] is None
    assert viewer_attempt["response_parsed"]


def test_json_repair_creates_a_separate_attempt(app, tmp_path) -> None:
    photo_id, traces, service = _analysis_fixture(app, tmp_path)
    provider = TraceProvider([{"invalid": True}, valid_result()])
    service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=provider,
        strategy="high_quality",
        high_model="trace-model",
    )
    rows = traces.list(filters={"photo_id": photo_id})
    detail = traces.detail(rows[0]["trace_id"], include_payloads=True)
    assert detail is not None
    assert [attempt["status"] for attempt in detail["attempts"]] == ["FAILED", "SUCCESS"]
    assert detail["attempts"][1]["retry_reason"] == "schema_validation_failed"
    assert provider.analyze_calls == 1
    assert provider.repair_calls == 1


def test_failed_provider_call_marks_failed_attempt_without_automatic_image_retry(app, tmp_path) -> None:
    photo_id, traces, service = _analysis_fixture(app, tmp_path)
    provider = FailedTraceProvider([])
    try:
        service.analyze_photo(
            photo_id=photo_id,
            job_id=None,
            provider=provider,
            strategy="high_quality",
            high_model="trace-model",
        )
    except ProviderHTTPError:
        pass
    else:
        raise AssertionError("failed provider must propagate")
    detail = traces.detail(traces.list(filters={"photo_id": photo_id})[0]["trace_id"], include_payloads=True)
    assert detail is not None
    assert detail["status"] == "FAILED"
    assert detail["attempts"][0]["http_status"] == 429
    assert detail["attempts"][0]["error_code"] == "VLM-002"
    assert provider.analyze_calls == 1


def test_trace_list_endpoint_is_hard_bounded(client, app, tmp_path) -> None:
    create_admin(app)
    login(client)
    photo_id, _traces, _service = _analysis_fixture(app, tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    with app.extensions["inktime_database"].transaction(operation="test.ai_trace_seed") as connection:
        connection.executemany(
            """
            INSERT INTO model_call_traces(
                trace_id,photo_id,provider,model,stage,status,started_at,completed_at,created_at
            ) VALUES (?,?, 'provider','model','single','SUCCESS',?,?,?)
            """,
            [(f"bounded-{index}", photo_id, now, now, now) for index in range(105)],
        )
    response = client.get("/api/v1/ai/traces?limit=1000")
    assert response.status_code == 200
    assert len(response.json["traces"]) == 100


def test_trace_persistence_failure_never_retries_provider_or_rolls_back_analysis(app, tmp_path) -> None:
    photo_id, _traces, service = _analysis_fixture(app, tmp_path)
    provider = TraceProvider([valid_result()])

    class BrokenTraceRepository:
        def __getattr__(self, _name):
            def fail(*_args, **_kwargs):
                raise RuntimeError("trace database unavailable")

            return fail

    service.traces = BrokenTraceRepository()
    service.analyze_photo(
        photo_id=photo_id,
        job_id=None,
        provider=provider,
        strategy="high_quality",
        high_model="trace-model",
    )
    assert provider.analyze_calls == 1
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute(
            "SELECT 1 FROM photo_analysis WHERE photo_id=?", (photo_id,)
        ).fetchone()
