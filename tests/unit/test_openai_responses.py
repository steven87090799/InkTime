from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from inktime.app.domain.analysis.schema import validate_model_response
from inktime.app.providers.base import IncompleteProviderResponse, Usage, assert_complete_response
from inktime.app.providers.config import normalize_options, validate_base_url
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError, calculate_usage_cost
from inktime.app.providers.openai_models import official_openai_pricing
from inktime.app.providers.openai_responses import OpenAIResponsesProvider, create_vision_provider
from inktime.app.workers.job_worker import BoundedJobWorker
from tests.unit.test_analysis_schema import valid_result


def wire_response(**updates):
    return {"id": "resp-test", "model": "gpt-6-luna", "status": "completed",
            "output": [{"type": "reasoning", "summary": []},
                       {"type": "message", "content": [{"type": "output_text", "text": json.dumps(valid_result())}]}],
            "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": 100, "output_tokens_details": {"reasoning_tokens": 40}}, **updates}


class Session:
    def __init__(self, payload=None, status=200, error=None):
        self.payload = wire_response() if payload is None else payload
        self.status, self.error, self.calls = status, error, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code = self.status
        response._content = json.dumps(self.payload).encode()
        response.headers["x-request-id"] = "req-safe"
        return response

    get = post

    def close(self):
        pass


def provider(session=None, **kwargs):
    return OpenAIResponsesProvider(name="OpenAI Direct", kind="openai", provider_id="direct-test",
                                   base_url="https://api.openai.com/v1", api_key="sk-private-test",
                                   session=session or Session(), pricing=official_openai_pricing(), **kwargs)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "synthetic.jpg"
    path.write_bytes(b"synthetic-image-test-payload")
    return path


def call(p, image, **kwargs):
    return p.analyze(image_path=image, model="gpt-6-luna", detail="high", stage="single", max_tokens=512, **kwargs)


def test_responses_reuses_schema_prompt_usage_and_normalized_photo(image):
    p = provider(options={"api_mode": "responses", "organization_id": "org-test", "project_id": "proj_test"})
    response = call(p, image)
    url, kwargs = p.session.calls[0]
    body = kwargs["json"]
    assert url == "https://api.openai.com/v1/responses"
    assert body["store"] is False and body["max_output_tokens"] == 512
    assert body["service_tier"] == "default"
    assert body["reasoning"] == {"effort": "none"}
    assert not {"messages", "response_format", "temperature", "max_completion_tokens", "tools"} & body.keys()
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert "content_filter" in body["text"]["format"]["schema"]["properties"]
    assert body["input"][1]["content"][1]["type"] == "input_image"
    assert body["input"][1]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    assert kwargs["headers"]["OpenAI-Organization"] == "org-test"
    assert kwargs["headers"]["OpenAI-Project"] == "proj_test"
    assert validate_model_response(response.content) == valid_result()
    assert response.usage == Usage(input_tokens=1000, output_tokens=100, reasoning_tokens=40)
    assert p.estimate_cost("gpt-6-luna", response.usage) == pytest.approx(0.00015)
    assert response.call_trace.api_mode == "responses"
    assert response.call_trace.endpoint.endswith("/responses")
    assert "base64" not in json.dumps(response.call_trace.request_json_sanitized)
    assert response.request_metrics["prompt_chars"] > 0


@pytest.mark.parametrize("code", ["credit_balance_exhausted", "insufficient_quota",
                                  "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
                                  "organization_usage_limit_exceeded"])
def test_billing_rejection_is_distinct_and_never_retried(image, code):
    p = provider(Session({"error": {"code": code}}, status=429))
    with pytest.raises(ProviderHTTPError) as error:
        call(p, image)
    assert error.value.code == "API_CREDIT_EXHAUSTED"
    assert error.value.ambiguous is False and error.value.request_started
    assert len(p.session.calls) == 1


@pytest.mark.parametrize(("status", "code", "expected"), [(401, "invalid_api_key", "AUTH_REQUIRED"),
    (403, "permission_denied", "AUTH_REQUIRED"), (404, "model_not_found", "CONFIG_INVALID"),
    (429, "rate_limit_exceeded", "VLM-002"), (429, "slow_down", "VLM-002"),
    (503, "server_is_overloaded", "VLM-007")])
def test_errors_keep_single_dispatch_and_do_not_log_sensitive_data(image, status, code, expected, caplog):
    caplog.set_level("DEBUG")
    p = provider(Session({"error": {"code": code, "message": "sk-private-test data:image/jpeg;base64,Zm9v"}}, status=status))
    with pytest.raises(ProviderHTTPError) as error:
        call(p, image)
    assert error.value.code == expected
    assert len(p.session.calls) == 1
    assert "sk-private-test" not in str(error.value.response_info)
    assert "base64" not in str(error.value.response_info)
    assert "sk-private-test" not in caplog.text and "Zm9v" not in caplog.text


def test_timeout_is_ambiguous_and_never_retried(image):
    p = provider(Session(error=requests.ReadTimeout()))
    with pytest.raises(ProviderHTTPError) as error:
        call(p, image)
    assert error.value.ambiguous and len(p.session.calls) == 1


@pytest.mark.parametrize("updates", [
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "refused"}]}]},
    {"status": "failed", "error": {"code": "server_error"}},
])
def test_incomplete_and_refused_responses_keep_usage_without_repair(image, updates):
    p = provider(Session(wire_response(**updates)))
    response = call(p, image)
    assert response.usage.output_tokens == 100
    with pytest.raises(IncompleteProviderResponse):
        assert_complete_response(response)
    assert len(p.session.calls) == 1


@pytest.mark.parametrize("payload", [{}, {"status": "completed", "output": {}}, {"status": "queued", "output": []}])
def test_invalid_envelope_is_held_without_resend(image, payload):
    p = provider(Session(payload))
    with pytest.raises(ProviderHTTPError) as error:
        call(p, image)
    assert error.value.ambiguous and len(p.session.calls) == 1


def test_model_effort_capabilities_and_provider_specific_override(image):
    p = provider(options={"api_mode": "responses", "reasoning_effort": "low"})
    call(p, image, reasoning_effort="none")
    assert p.session.calls[0][1]["json"]["reasoning"] == {"effort": "low"}
    for model in ("gpt-unknown", "gpt-6-astra"):
        with pytest.raises(ValueError, match="PROVIDER-023"):
            provider().build_analysis_request_body(image_path=image, model=model, detail="high", stage="single")


def test_json_repair_is_text_only_and_bounded(image):
    p = provider()
    p.repair_json(invalid_content="{broken", validation_error="invalid", model="gpt-6-luna", max_tokens=512)
    body = p.session.calls[0][1]["json"]
    assert body["store"] is False and body["max_output_tokens"] == 512
    assert "input_image" not in json.dumps(body)
    assert p.last_request_metrics["image_bytes"] == 0


def test_batch_create_and_requests_use_responses(image):
    p = provider(Session({"id": "batch-test"}))
    body = p.build_analysis_request_body(image_path=image, model="gpt-6-luna", detail="high", stage="single")
    assert "service_tier" not in body
    p.create_batch("file-test")
    assert p.session.calls[0][1]["json"]["endpoint"] == "/v1/responses"


def test_official_account_models_intersect_capabilities_without_image_call():
    p = provider(Session({"data": [{"id": "gpt-6-luna"}, {"id": "unverified-model"}]}))
    models = p.available_models()
    assert [m["id"] for m in models if m["available"]] == ["gpt-6-luna"]
    assert p.session.calls[0][0].endswith("/models")
    assert "json" not in p.session.calls[0][1]


def test_openrouter_remains_chat_completions_and_separate_options(image):
    session = Session({"choices": [{"message": {"content": "{}"}}]})
    p = create_vision_provider(name="OpenRouter", kind="openrouter", base_url="https://openrouter.ai/api/v1",
                              api_key="router-secret", session=session, options={"allow_fallbacks": False})
    p.analyze(image_path=image, model="openrouter/free", stage="single", detail="high", max_tokens=512)
    url, request = session.calls[0]
    assert url.endswith("/chat/completions") and "messages" in request["json"]
    assert request["headers"]["Authorization"] == "Bearer router-secret"
    assert "store" not in request["json"]
    with pytest.raises(ValueError):
        normalize_options("openrouter", {"organization_id": "org-test"})
    with pytest.raises(ValueError):
        validate_base_url("openai", "https://untrusted.invalid/v1")


@pytest.mark.parametrize("status", [200, 429, 503])
def test_sdk_payload_headers_no_env_credentials_no_retries(image, monkeypatch, status):
    import httpx2
    import openai

    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        return httpx2.Response(status, json=wire_response() if status == 200
                              else {"error": {"code": "credit_balance_exhausted", "message": "limited"}})

    monkeypatch.setenv("OPENAI_ORG_ID", "org-wrong-environment")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-wrong-environment")
    monkeypatch.setattr(openai, "DefaultHttpxClient", lambda **kwargs: httpx2.Client(
        transport=httpx2.MockTransport(handler), **kwargs))
    p = OpenAIResponsesProvider(name="SDK", kind="openai", base_url="https://api.openai.com/v1",
                               api_key="explicit-key", options={"api_mode": "responses", "organization_id": "org-explicit"})
    try:
        assert p.session.client.max_retries == 0
        if status == 200:
            response = call(p, image)
            assert response.usage.input_tokens == 1000
        else:
            with pytest.raises(ProviderHTTPError) as error:
                call(p, image)
            assert error.value.code == ("API_CREDIT_EXHAUSTED" if status == 429 else "VLM-007")
        assert len(requests_seen) == 1
        request = requests_seen[0]
        assert request.headers["authorization"] == "Bearer explicit-key"
        assert request.headers["openai-organization"] == "org-explicit"
        assert "openai-project" not in request.headers
        assert json.loads(request.content)["store"] is False
    finally:
        p.close()


def test_process_spec_restores_responses_adapter_and_independent_options():
    p = provider(options={"api_mode": "responses", "project_id": "proj_saved", "reasoning_effort": "low"})
    child = OpenAICompatibleProvider.from_process_spec(p.process_spec())
    try:
        assert isinstance(child, OpenAIResponsesProvider)
        assert child.options["project_id"] == "proj_saved"
    finally:
        child.close()


def test_legacy_official_endpoint_suffix_keeps_normalizing_to_same_origin():
    p = OpenAICompatibleProvider(name="legacy", kind="openai", api_key="legacy-key",
                                 base_url="https://api.openai.com/v1/chat/completions", session=Session())
    assert p.base_url == "https://api.openai.com/v1" and p.api_mode == "chat_completions"


def test_legacy_openai_chat_also_disables_storage_without_changing_openrouter(image):
    p = OpenAICompatibleProvider(name="legacy", kind="openai", api_key="legacy-key",
                                 base_url="https://api.openai.com/v1", session=Session())
    body = p.build_analysis_request_body(image_path=image, model="gpt-4o", detail="high", stage="single")
    assert body["store"] is False and "messages" in body


def test_worker_credit_pause_defers_photo_without_failing_or_losing_progress():
    repository = SimpleNamespace(defer_item=Mock(return_value=True), request_pause=Mock(), add_event=Mock(), fail_item=Mock())
    worker = BoundedJobWorker(repository, lambda _: {})
    worker._record_failure("job", "photo-item", ProviderHTTPError("credits", "API_CREDIT_EXHAUSTED"))
    repository.defer_item.assert_called_once()
    repository.request_pause.assert_called_once_with("job")
    repository.fail_item.assert_not_called()


def test_cache_writes_and_reasoning_are_not_double_counted_and_unknown_prices_stay_unknown():
    prices = official_openai_pricing()["gpt-6-luna"]
    usage = Usage(input_tokens=12000, output_tokens=100, cached_tokens=4000, cache_write_tokens=7000, reasoning_tokens=40)
    expected = (1000 * .10 + 4000 * .01 + 7000 * .125 + 100 * .50) / 1_000_000
    assert calculate_usage_cost(prices, usage) == pytest.approx(expected)
    assert calculate_usage_cost(prices, usage, batch=True) == pytest.approx(expected / 2)
    assert calculate_usage_cost({"input_per_million": .1, "output_per_million": .5}, usage) is None
