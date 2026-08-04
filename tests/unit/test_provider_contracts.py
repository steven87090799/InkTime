from __future__ import annotations

import json
from pathlib import Path

from inktime.app.providers.base import ProviderResponse, Usage
from inktime.app.services.provider_contracts import run_provider_contract
from tests.unit.test_analysis_schema import valid_result


class FakeContractProvider:
    def __init__(self, *, valid: bool = True):
        self.valid = valid
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
    assert len(provider.repair_calls) == 1
    assert "image_path" not in provider.repair_calls[0]
    assert result["usage"]["cost_source"] == "provider_reported"
