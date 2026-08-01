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
