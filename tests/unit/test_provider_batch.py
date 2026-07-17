from __future__ import annotations

from inktime.app.providers.openai_compatible import OpenAICompatibleProvider


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
    provider = OpenAICompatibleProvider(name="OpenAI", base_url="https://api.openai.com/v1", api_key="secret", session=session)
    batch_id = provider.submit_batch([{"custom_id": "photo-1", "body": {"model": "vision"}}])
    assert batch_id == "batch-123"
    upload = session.calls[0][1]
    assert upload["data"]["purpose"] == "batch"
    assert b'"custom_id":"photo-1"' in upload["files"]["file"][1]
    creation = session.calls[1][1]["json"]
    assert creation == {"input_file_id": "file-123", "endpoint": "/v1/chat/completions", "completion_window": "24h"}
