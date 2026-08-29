from __future__ import annotations

import json
from pathlib import Path

import pytest

from inktime.app.providers.base import ProviderResponse, Usage
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError


class CaptureProvider(OpenAICompatibleProvider):
    def _post_completion(self, body, *, vision_attempt=None, retry_policy=None):
        self.captured_body = body
        return ProviderResponse("{}", Usage())


def _provider(kind: str = "openrouter", options: dict | None = None) -> CaptureProvider:
    return CaptureProvider(
        name="contract-provider",
        base_url="https://openrouter.ai/api/v1" if kind == "openrouter" else "https://api.example.invalid/v1",
        api_key="redacted-test-key",
        kind=kind,
        options=options or {},
        supports_reasoning_effort=kind == "openai",
    )


def test_openrouter_analysis_and_repair_share_privacy_routing_usage_and_sticky_policy(tmp_path: Path):
    image = tmp_path / "synthetic.jpg"
    image.write_bytes(b"synthetic image")
    options = {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "only": ["provider/model"],
        "session_sticky": True,
    }
    provider = _provider(options=options)
    analysis = provider.build_analysis_request_body(
        image_path=image,
        model="provider/vision-model",
        detail="high",
        stage="single",
        reasoning_effort="low",
        provider_request_context_id="job|photo|vision-fingerprint|owner-hash",
    )
    provider.repair_json(
        invalid_content="not-json",
        validation_error="schema",
        model="provider/repair-model",
        stage="single",
        provider_request_context_id="job|photo|vision-fingerprint|owner-hash",
    )
    repair = provider.captured_body

    for body in (analysis, repair):
        assert body["provider"]["data_collection"] == "deny"
        assert body["provider"]["zdr"] is True
        assert body["provider"]["require_parameters"] is True
        assert body["provider"]["allow_fallbacks"] is False
        assert body["usage"] == {"include": True}
    assert analysis["reasoning"] == {"effort": "low"}
    assert "reasoning" not in repair
    assert repair["messages"][1]["content"]
    assert "image_url" not in json.dumps(repair, ensure_ascii=False)
    assert "image_path" not in json.dumps(repair, ensure_ascii=False)
    assert provider.last_request_metrics["image_bytes"] == 0
    assert analysis["session_id"] == repair["session_id"]


def test_openrouter_analysis_explicitly_disables_reasoning_when_requested(tmp_path: Path):
    image = tmp_path / "synthetic.jpg"
    image.write_bytes(b"synthetic image")

    body = _provider().build_analysis_request_body(
        image_path=image,
        model="provider/vision-model",
        detail="high",
        stage="provider_contract_level2",
        reasoning_effort="none",
    )

    assert body["reasoning"] == {"effort": "none"}


@pytest.mark.parametrize(
    ("filename", "expected_media_type"),
    [("synthetic.jpg", "image/jpeg"), ("synthetic.png", "image/png")],
)
def test_analysis_data_url_matches_image_file_format(tmp_path: Path, filename, expected_media_type):
    image = tmp_path / filename
    image.write_bytes(b"synthetic image")
    body = _provider().build_analysis_request_body(
        image_path=image,
        model="provider/vision-model",
        detail="high",
        stage="single",
    )

    image_url = body["messages"][1]["content"][1]["image_url"]["url"]
    assert image_url.startswith(f"data:{expected_media_type};base64,")


def test_openai_compatible_request_does_not_receive_openrouter_provider_object(tmp_path: Path):
    image = tmp_path / "synthetic.jpg"
    image.write_bytes(b"synthetic image")
    body = _provider(kind="openai_compatible").build_analysis_request_body(
        image_path=image,
        model="provider/vision-model",
        detail="high",
        stage="single",
    )
    assert "provider" not in body
    assert "usage" not in body


def test_unknown_openrouter_option_is_rejected():
    with pytest.raises(ValueError, match="PROVIDER-007"):
        _provider(options={"unknown_policy": True})


def test_openrouter_400_is_not_retried_and_preserves_bounded_safe_error_details():
    class Response:
        status_code = 400
        headers = {"x-openrouter-request-id": "request-safe-123"}
        text = json.dumps(
            {
                "error": {
                    "code": "invalid_model",
                    "message": "Unknown model; api_key=super-secret data:image/png;base64,QUJDREVGRw==",
                }
            }
        )

        def json(self):
            return json.loads(self.text)

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

        def close(self):
            return None

    session = Session()
    provider = OpenAICompatibleProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="super-secret",
        kind="openrouter",
        session=session,
    )
    with pytest.raises(ProviderHTTPError) as caught:
        provider._post_completion({"model": "openai/gpt-4o", "messages": []})

    error = caught.value
    assert len(session.calls) == 1
    assert error.code == "CONFIG_INVALID"
    assert error.http_status == 400
    assert error.provider_error_code == "invalid_model"
    assert error.response_info["request_id"] == "request-safe-123"
    assert "super-secret" not in str(error.response_info)
    assert "QUJDREVGRw" not in str(error.response_info)
    assert error.call_trace is not None
    assert error.call_trace.http_status == 400
    provider.close()
