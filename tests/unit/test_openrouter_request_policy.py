from __future__ import annotations

import json
from pathlib import Path

import pytest

from inktime.app.providers.base import ProviderResponse, Usage
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError
from inktime.app.providers.openai_compatible import calculate_usage_cost
from inktime.app.services.batch_analysis import BatchAnalysisService


@pytest.mark.parametrize("payload", [{}, {"usage": None}, {"usage": {}}, {"usage": {"prompt_tokens": 12}}])
def test_missing_usage_never_becomes_zero_cost(payload):
    for usage in (OpenAICompatibleProvider._usage(payload), BatchAnalysisService._usage_from_body(payload)):
        assert not usage.tokens_reported
        assert calculate_usage_cost({}, usage) is None
        assert calculate_usage_cost({}, usage, batch=True) is None


def test_explicit_zero_usage_and_provider_cost_remain_distinct():
    zero = OpenAICompatibleProvider._usage({"usage": {"prompt_tokens": 0, "completion_tokens": 0}})
    assert zero.tokens_reported
    assert calculate_usage_cost({}, zero) == 0
    reported = OpenAICompatibleProvider._usage({"usage": {"cost": 0.05}})
    assert not reported.tokens_reported
    assert reported.provider_reported_cost == 0.05


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
        immutable_semantic_values={"memory_score": 72},
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
        assert "uniqueItems" not in json.dumps(body["response_format"], ensure_ascii=False)
    assert analysis["reasoning"] == {"effort": "low"}
    assert repair["reasoning"] == {"effort": "none"}
    assert repair["messages"][1]["content"]
    assert json.loads(repair["messages"][1]["content"])["immutable_semantic_values"] == {
        "memory_score": 72
    }
    assert "image_url" not in json.dumps(repair, ensure_ascii=False)
    assert "image_path" not in json.dumps(repair, ensure_ascii=False)
    assert "uniqueItems" not in repair["messages"][1]["content"]
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
    assert "uniqueItems" in json.dumps(body["response_format"], ensure_ascii=False)


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


def test_openrouter_no_compatible_endpoint_404_is_retryable():
    class Response:
        status_code = 404
        headers = {}
        text = json.dumps(
            {
                "error": {
                    "code": 404,
                    "message": "No endpoints found that can handle the requested parameters.",
                }
            }
        )

        def json(self):
            return json.loads(self.text)

    class Session:
        def post(self, _url, **_kwargs):
            return Response()

        def close(self):
            return None

    provider = OpenAICompatibleProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="redacted-test-key",
        kind="openrouter",
        session=Session(),
    )
    with pytest.raises(ProviderHTTPError) as caught:
        provider._post_completion({"model": "openrouter/free", "messages": []})

    error = caught.value
    assert error.code == "VLM-005"
    assert error.http_status == 404
    assert error.response_info["provider_error_code"] == "404"
    assert "No endpoints found" in error.response_info["provider_error_message"]
    provider.close()


def test_disabled_structured_output_includes_complete_text_schema(tmp_path):
    from inktime.app.domain.analysis.schema import json_schema_for_stage

    image = tmp_path / "synthetic.jpg"
    image.write_bytes(b"synthetic")
    provider = _provider(kind="openai_compatible")
    provider.supports_json_schema = False
    body = provider.build_analysis_request_body(
        image_path=image, model="vision-model", detail="high", stage="single",
    )
    assert "response_format" not in body
    prompt = body["messages"][0]["content"]
    schema = json.loads(prompt.split("完整 JSON Schema：", 1)[1])
    assert schema == json_schema_for_stage("single")["schema"]


@pytest.mark.parametrize("model", ["o1", "o3", "o3-2025-04-16", "o4-mini"])
def test_o_series_uses_completion_cap_without_temperature(tmp_path, model):
    image = tmp_path / "synthetic.jpg"
    image.write_bytes(b"synthetic")
    provider = _provider(kind="openai")
    body = provider.build_analysis_request_body(
        image_path=image, model=model, detail="high", stage="single",
        max_tokens=512, reasoning_effort="low",
    )
    assert body["max_completion_tokens"] == 512
    assert "max_tokens" not in body and "temperature" not in body
    with pytest.raises(ValueError, match="不能關閉推理"):
        provider.repair_json(invalid_content="{}", validation_error="invalid", model=model, max_tokens=512)


def test_text_only_model_is_rejected_before_reading_image(tmp_path):
    with pytest.raises(ValueError, match="不支援圖片"):
        _provider(kind="openai").build_analysis_request_body(
            image_path=tmp_path / "missing.jpg", model="o3-mini", detail="high", stage="single",
        )


@pytest.mark.parametrize("invalid", [True, -1, "invalid", "12", 1.5, float("nan"), float("inf"), 2**63])
def test_invalid_wire_usage_is_unknown_in_sync_and_batch(invalid):
    payload = {"usage": {"prompt_tokens": invalid, "completion_tokens": 0}}
    for usage in (OpenAICompatibleProvider._usage(payload), BatchAnalysisService._usage_from_body(payload)):
        assert not usage.tokens_reported
        assert calculate_usage_cost({"input_per_million": 1, "output_per_million": 1}, usage) is None
        assert calculate_usage_cost({}, usage, batch=True) is None
    payload["usage"]["cost"] = 0.02
    assert OpenAICompatibleProvider._usage(payload).provider_reported_cost == 0.02


@pytest.mark.parametrize("details", [
    {"prompt_tokens_details": {"cached_tokens": 11}},
    {"completion_tokens_details": {"reasoning_tokens": 3}},
    {"prompt_tokens_details": {"cached_tokens": -1}},
    {"completion_tokens_details": []},
    {"input_tokens_details": "invalid"},
])
def test_invalid_usage_details_cannot_make_a_known_estimate(details):
    payload = {"usage": {"prompt_tokens": 10, "completion_tokens": 2, **details}}
    for usage in (OpenAICompatibleProvider._usage(payload), BatchAnalysisService._usage_from_body(payload)):
        assert not usage.tokens_reported
        assert calculate_usage_cost({}, usage) is None


def test_cost_counts_cached_input_once_and_reasoning_as_part_of_output():
    pricing = {"input_per_million": 1, "cached_input_per_million": 0.1, "output_per_million": 2}
    usage = Usage(input_tokens=10, output_tokens=5, cached_tokens=4, reasoning_tokens=3)
    assert calculate_usage_cost(pricing, usage) == 0.0000164
    assert calculate_usage_cost(pricing, usage, batch=True) == 0.0000082
    assert calculate_usage_cost(pricing, Usage(input_tokens=1, cached_tokens=2)) is None
    assert calculate_usage_cost(pricing, Usage(input_tokens=-1)) is None
    assert calculate_usage_cost(pricing, Usage(input_tokens=1, cache_write_tokens=1)) is None
    assert calculate_usage_cost({"input_per_million": 0.1, "output_per_million": 0.2}, Usage(1, 1)) == 0.0000003


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1-mini"])
def test_nonreasoning_openai_model_omits_reasoning_parameter(tmp_path, model):
    image = tmp_path / "synthetic.jpg"
    image.write_bytes(b"synthetic")
    body = _provider(kind="openai").build_analysis_request_body(
        image_path=image, model=model, detail="high", stage="single", max_tokens=512, reasoning_effort="none",
    )
    assert "reasoning_effort" not in body
    assert body["max_tokens"] == 512


@pytest.mark.parametrize("model", ["gpt-5.1", "gpt-5.1-2025-11-13", "gpt-5.6-luna", "gpt-6-luna"])
def test_supported_nonreasoning_modern_openai_request_uses_completion_cap(tmp_path, model):
    image = tmp_path / "synthetic.jpg"
    image.write_bytes(b"synthetic")
    provider = _provider(kind="openai")
    body = provider.build_analysis_request_body(
        image_path=image, model=model, detail="high", stage="single", max_tokens=512, reasoning_effort="none",
    )
    assert body["max_completion_tokens"] == 512 and "max_tokens" not in body
    assert body["reasoning_effort"] == "none"
    provider.repair_json(invalid_content="{}", validation_error="invalid", model=model, max_tokens=512)
    assert provider.captured_body["reasoning_effort"] == "none"


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5-mini", "gpt-5-2025-08-07", "gpt-6-astra", "gpt-6.1-sol"])
def test_mandatory_reasoning_model_does_not_silently_spend_reasoning_tokens(tmp_path, model):
    image = tmp_path / "synthetic.jpg"
    image.write_bytes(b"synthetic")
    with pytest.raises(ValueError, match="不能關閉推理"):
        _provider(kind="openai").build_analysis_request_body(
            image_path=image, model=model, detail="high", stage="single", max_tokens=512, reasoning_effort="none",
        )


@pytest.mark.parametrize("model", ["gpt-5.2-pro", "gpt-5.2-codex"])
def test_responses_only_model_is_rejected_before_image_access(tmp_path, model):
    with pytest.raises(ValueError, match="Responses API"):
        _provider(kind="openai").build_analysis_request_body(
            image_path=tmp_path / "missing.jpg", model=model, detail="high", stage="single", max_tokens=512,
        )


@pytest.mark.parametrize("version, legacy", [
    ("photo-analysis-v5-compact", True),
    ("photo-analysis-v5-compact-caption-abc123", True),
    ("photo-analysis-v5-compact-literary-20261011", False),
])
def test_frozen_prompt_version_matches_actual_wire_contract(tmp_path, version, legacy):
    from inktime.app.providers.openai_compatible import COMMON_PROMPT, LEGACY_V5_COMMON_PROMPT
    image = tmp_path / "synthetic.jpg"
    image.write_bytes(b"synthetic")
    body = _provider(kind="openai").build_analysis_request_body(
        image_path=image, model="gpt-4o", detail="high", stage="single", prompt_version=version,
    )
    assert body["messages"][0]["content"].startswith(LEGACY_V5_COMMON_PROMPT if legacy else COMMON_PROMPT)
