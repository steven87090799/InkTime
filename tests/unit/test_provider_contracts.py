from __future__ import annotations

import json
from pathlib import Path

from inktime.app.providers.base import ProviderResponse, Usage
from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.services.provider_contracts import run_provider_contract
from tests.unit.test_analysis_schema import valid_result


class FakeContractProvider:
    def __init__(self, *, valid: bool = True):
        self.valid = valid
        self.kind = "openrouter"
        self.options = {"data_collection": "deny", "zdr": True}
        self.analyze_calls: list[dict] = []
        self.repair_calls: list[dict] = []

    def validate_config(self):
        return True, "ok"

    def analyze(self, **kwargs):
        self.analyze_calls.append(kwargs)
        image = Path(kwargs["image_path"])
        assert image.name == "inktime-test.png"
        assert image.stat().st_size > 0
        if kwargs["stage"] == "provider_contract_level2" and self.valid:
            value = {"vision_ok": True, "detected_shapes": ["rectangle", "circle"]}
        elif self.valid:
            value = valid_result()
        else:
            value = "not-json"
        return ProviderResponse(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            Usage(input_tokens=20, output_tokens=10, provider_reported_cost=0.08),
        )

    def repair_json(self, **kwargs):
        self.repair_calls.append(kwargs)
        return ProviderResponse(json.dumps(valid_result(), ensure_ascii=False), Usage(input_tokens=5, output_tokens=3))

    def estimate_cost(self, model, usage):
        return None


class RepairFailureProvider(FakeContractProvider):
    def repair_json(self, **kwargs):
        self.repair_calls.append(kwargs)
        raise TimeoutError("repair timeout")


class AmbiguousRepairFailureProvider(FakeContractProvider):
    def repair_json(self, **kwargs):
        self.repair_calls.append(kwargs)
        raise ProviderHTTPError(
            "repair response ambiguous",
            "AMBIGUOUS_REPAIR",
            ambiguous=True,
            http_status=500,
        )


class CostedRepairProvider(FakeContractProvider):
    def repair_json(self, **kwargs):
        self.repair_calls.append(kwargs)
        return ProviderResponse(
            json.dumps(valid_result(), ensure_ascii=False),
            Usage(input_tokens=5, output_tokens=3, provider_reported_cost=0.02),
        )


class RejectedVisionProvider(FakeContractProvider):
    def analyze(self, **kwargs):
        raise ProviderHTTPError(
            "Provider rejected request",
            "CONFIG_INVALID",
            http_status=400,
            provider_error_code="invalid_model",
            response_info={
                "provider_error_code": "invalid_model",
                "provider_error_message": (
                    "Unknown model api_key=super-secret data:image/png;base64,QUJDREVGRw=="
                ),
                "request_id": "safe-request-123",
            },
        )


def test_level1_is_connection_only():
    provider = FakeContractProvider()
    result = run_provider_contract(provider, level=1, model="model")
    assert result["ok"] is True
    assert result["network_requests"] == 1
    assert provider.analyze_calls == []


def test_level2_sends_one_256_token_synthetic_image_without_repair():
    provider = FakeContractProvider()
    result = run_provider_contract(provider, level=2, model="model")
    assert result["ok"] is True
    assert result["vision_requests"] == 1
    assert result["repair_requests"] == 0
    assert provider.analyze_calls[0]["max_tokens"] == 256


def test_level3_allows_only_one_text_repair_and_returns_safe_usage():
    provider = FakeContractProvider(valid=False)
    result = run_provider_contract(provider, level=3, model="model")
    assert result["vision_requests"] == 1
    assert result["repair_requests"] == 1
    assert result["repair_attempts"] == 1
    assert result["repair_responses"] == 1
    assert result["network_request_attempts"] == 2
    assert result["network_responses"] == 2
    assert len(provider.repair_calls) == 1
    assert "image_path" not in provider.repair_calls[0]
    assert result["usage"]["cost_source"] == "unknown"
    assert result["usage"]["unknown_cost_count"] == 1
    assert provider.analyze_calls[0]["reasoning_effort"] == "none"
    assert result["checks"]["privacy_policy"] == {
        "data_collection": "deny",
        "zdr": True,
        "configured": True,
    }


def test_level3_repair_timeout_counts_attempt_without_response_and_hides_content():
    provider = RepairFailureProvider(valid=False)
    result = run_provider_contract(provider, level=3, model="model")
    assert result["ok"] is False
    assert result["repair_attempts"] == 1
    assert result["repair_requests"] == 1
    assert result["repair_responses"] == 0
    assert result["network_request_attempts"] == 2
    assert result["network_responses"] == 1
    assert result["repair_completed"] is False
    assert result["usage"]["cost_source"] == "unknown"
    assert result["usage"]["unknown_cost_count"] == 1
    assert "not-json" not in result["message"]


def test_level3_ambiguous_repair_error_counts_attempt_without_response():
    provider = AmbiguousRepairFailureProvider(valid=False)
    result = run_provider_contract(provider, level=3, model="model")
    assert result["ok"] is False
    assert result["vision_requests"] == 1
    assert result["repair_attempts"] == 1
    assert result["repair_requests"] == 1
    assert result["repair_responses"] == 0
    assert result["network_request_attempts"] == 2
    assert result["network_responses"] == 1
    assert result["repair_completed"] is False
    assert result["usage"]["unknown_cost_count"] == 1


def test_level3_repair_usage_is_aggregated_without_second_vision_image():
    provider = CostedRepairProvider(valid=False)
    result = run_provider_contract(provider, level=3, model="model")
    assert result["ok"] is True
    assert result["repair_attempts"] == 1
    assert result["repair_responses"] == 1
    assert result["network_request_attempts"] == 2
    assert result["network_responses"] == 2
    assert round(result["usage"]["provider_reported_cost"], 6) == 0.1
    assert result["usage"]["unknown_cost_count"] == 0
    assert "image_path" not in provider.repair_calls[0]
    assert len(provider.analyze_calls) == 1


def test_contract_failure_returns_safe_actionable_provider_error():
    result = run_provider_contract(RejectedVisionProvider(), level=2, model="openai/gpt-4o")

    assert result["ok"] is False
    assert result["provider_error"] == {
        "error_code": "CONFIG_INVALID",
        "http_status": 400,
        "provider_error_code": "invalid_model",
        "provider_error_message": "Unknown model api_key=[已遮蔽] [已遮蔽圖片資料]",
        "request_id": "safe-request-123",
    }
    assert "CONFIG_INVALID / HTTP 400 / invalid_model / Unknown model" in result["message"]
    assert "super-secret" not in str(result)
    assert "QUJDREVGRw" not in str(result)
