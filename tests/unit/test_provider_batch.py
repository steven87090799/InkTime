from __future__ import annotations

from pathlib import Path

import requests
import pytest

from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.providers.openai_compatible import ProviderHTTPError


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/files"):
            return FakeResponse({"id": "file-123"})
        return FakeResponse({"id": "batch-123"})


def test_batch_uploads_jsonl_then_creates_batch():
    session = FakeSession()
    provider = OpenAICompatibleProvider(
        name="OpenAI", base_url="https://api.openai.com/v1", api_key="secret", session=session
    )
    batch_id = provider.submit_batch([{"custom_id": "photo-1", "body": {"model": "vision"}}])
    assert batch_id == "batch-123"
    upload = session.calls[0][1]
    assert upload["data"]["purpose"] == "batch"
    assert b'"custom_id":"photo-1"' in upload["files"]["file"][1]
    creation = session.calls[1][1]["json"]
    assert creation == {
        "input_file_id": "file-123",
        "endpoint": "/v1/chat/completions",
        "completion_window": "24h",
    }


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(86400, 86400), (3599, 3600), (2592001, 2592000)],
)
def test_create_batch_sends_openai_output_expiration_contract(configured, expected):
    session = FakeSession()
    provider = OpenAICompatibleProvider(
        name="OpenAI", base_url="https://api.openai.com/v1", api_key="secret", session=session
    )

    provider.create_batch("file-123", output_expires_after_seconds=configured)

    assert session.calls[-1][0].endswith("/batches")
    assert session.calls[-1][1]["json"]["output_expires_after"] == {
        "anchor": "created_at",
        "seconds": expected,
    }


class TimeoutOnBatchSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.batch_attempts = 0

    def post(self, url, **kwargs):
        if url.endswith("/batches"):
            self.batch_attempts += 1
            raise requests.Timeout("response lost after remote creation")
        return super().post(url, **kwargs)


def test_create_batch_timeout_is_ambiguous_and_never_retried():
    session = TimeoutOnBatchSession()
    provider = OpenAICompatibleProvider(
        name="OpenAI", base_url="https://api.openai.com/v1", api_key="secret", session=session
    )

    with pytest.raises(ProviderHTTPError) as raised:
        provider.create_batch("file-123")

    assert raised.value.code == "BATCH-SUBMISSION-UNKNOWN"
    assert raised.value.ambiguous is True
    assert session.batch_attempts == 1


class CompletionSession(FakeSession):
    def post(self, url, **kwargs):
        if url.endswith("/chat/completions"):
            self.calls.append((url, kwargs))
            return FakeResponse(
                {
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {},
                }
            )
        return super().post(url, **kwargs)


class VisionTimeoutSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.vision_attempts = 0

    def post(self, url, **kwargs):
        if url.endswith("/chat/completions"):
            self.vision_attempts += 1
            raise requests.Timeout("response lost after vision POST")
        return super().post(url, **kwargs)


def test_vision_post_timeout_is_ambiguous_and_never_retried(tmp_path):
    image = Path(tmp_path) / "vision-test.jpg"
    image.write_bytes(b"jpeg-fixture")
    session = VisionTimeoutSession()
    provider = OpenAICompatibleProvider(
        name="OpenAI", base_url="https://api.openai.com/v1", api_key="secret", session=session
    )

    with pytest.raises(ProviderHTTPError) as raised:
        provider.analyze(image_path=image, model="vision", detail="high", stage="single_high")

    assert raised.value.code == "VLM-AMBIGUOUS"
    assert raised.value.ambiguous is True
    assert session.vision_attempts == 1


def test_reasoning_effort_is_capability_gated_and_sync_uses_same_builder(tmp_path):
    image = Path(tmp_path) / "provider-test.jpg"
    image.write_bytes(b"jpeg-fixture")
    official_session = CompletionSession()
    official = OpenAICompatibleProvider(
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key="secret",
        supports_reasoning_effort=True,
        session=official_session,
    )
    batch_body = official.build_analysis_request_body(
        image_path=image,
        model="gpt-5.6-luna",
        detail="high",
        stage="single_high",
        max_tokens=8000,
        reasoning_effort="none",
    )
    official.analyze(
        image_path=image,
        model="gpt-5.6-luna",
        detail="high",
        stage="single_high",
        max_tokens=8000,
        reasoning_effort="none",
    )
    sync_body = official_session.calls[-1][1]["json"]
    assert batch_body == sync_body
    assert sync_body["reasoning_effort"] == "none"

    compatible = OpenAICompatibleProvider(
        name="Compatible",
        base_url="https://compatible.invalid/v1",
        api_key="secret",
        supports_reasoning_effort=False,
    )
    compatible_body = compatible.build_analysis_request_body(
        image_path=image,
        model="vision",
        detail="high",
        stage="single_high",
        reasoning_effort="none",
    )
    assert "reasoning_effort" not in compatible_body


class StatusSession(FakeSession):
    def __init__(self, status_code, payload=None, *, error=None):
        super().__init__()
        self.status_code = status_code
        self.payload = payload if payload is not None else {"error": {"code": "provider_error"}}
        self.error = error

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload, self.status_code)


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422, 429])
def test_create_batch_definite_http_rejections_are_not_ambiguous(status_code):
    session = StatusSession(status_code)
    provider = OpenAICompatibleProvider(
        name="OpenAI", base_url="https://api.openai.com/v1", api_key="secret", session=session
    )
    with pytest.raises(ProviderHTTPError) as raised:
        provider.create_batch("file-123")
    assert raised.value.http_status == status_code
    assert raised.value.ambiguous is False
    assert raised.value.provider_error_code == "provider_error"
    assert "secret" not in str(raised.value)
    if status_code == 429:
        assert raised.value.code == "BATCH-RATE-LIMITED"
    assert len(session.calls) == 1


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_create_batch_unknown_5xx_is_ambiguous_and_not_retried(status_code):
    session = StatusSession(status_code)
    provider = OpenAICompatibleProvider(
        name="OpenAI", base_url="https://api.openai.com/v1", api_key="secret", session=session
    )
    with pytest.raises(ProviderHTTPError) as raised:
        provider.create_batch("file-123")
    assert raised.value.http_status == status_code
    assert raised.value.ambiguous is True
    assert raised.value.code == "BATCH-SUBMISSION-UNKNOWN"
    assert len(session.calls) == 1


def test_create_batch_invalid_json_or_missing_id_is_ambiguous():
    invalid = StatusSession(200)
    invalid.payload = object()
    invalid_response = FakeResponse({"not": "json"})
    invalid_response.json = lambda: (_ for _ in ()).throw(ValueError("bad json"))
    invalid.post = lambda url, **kwargs: invalid_response
    provider = OpenAICompatibleProvider(
        name="OpenAI", base_url="https://api.openai.com/v1", api_key="secret", session=invalid
    )
    with pytest.raises(ProviderHTTPError) as raised:
        provider.create_batch("file-123")
    assert raised.value.ambiguous is True
    missing = StatusSession(200, {})
    provider = OpenAICompatibleProvider(
        name="OpenAI", base_url="https://api.openai.com/v1", api_key="secret", session=missing
    )
    with pytest.raises(ProviderHTTPError) as raised:
        provider.create_batch("file-123")
    assert raised.value.code == "BATCH-SUBMISSION-UNKNOWN"
    assert raised.value.ambiguous is True


@pytest.mark.parametrize(
    ("outcome", "expected_ambiguous"),
    [
        (requests.Timeout("after remote create"), True),
        (requests.ConnectionError("connection reset"), True),
        ("500", True),
        ("429", False),
        ("400", False),
    ],
)
def test_upload_file_side_effect_is_never_retried(tmp_path, outcome, expected_ambiguous):
    path = tmp_path / "input.jsonl"
    path.write_text('{"custom_id":"ibt:00000000-0000-0000-0000-000000000000"}\n')
    if isinstance(outcome, Exception):
        session = StatusSession(200, error=outcome)
    else:
        session = StatusSession(int(outcome))
    provider = OpenAICompatibleProvider(
        name="OpenAI", base_url="https://api.openai.com/v1", api_key="secret", session=session
    )
    with pytest.raises(ProviderHTTPError) as raised:
        provider.upload_batch_file(path, remote_filename="inktime-batch-anonymous.jsonl")
    assert raised.value.ambiguous is expected_ambiguous
    assert session.calls[0][1]["files"]["file"][0] == "inktime-batch-anonymous.jsonl"
    assert "secret" not in str(raised.value)
    assert len(session.calls) == 1
