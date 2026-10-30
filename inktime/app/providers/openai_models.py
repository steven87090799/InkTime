"""Official vision/Responses capabilities, verified 2026-10-30.

This is a capability catalog, never evidence of account access. Intersect with
GET /models before offering account-verified choices. Prices are estimates in
USD per million tokens, before credits; see https://developers.openai.com/api/docs/pricing.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_OPENAI_MODEL = "gpt-6-luna"
OPENAI_MODELS: dict[str, dict[str, Any]] = {
    "gpt-6-luna": {"reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
                   "input": 0.10, "cached": 0.01, "output": 0.50},
    "gpt-6.1-terra": {"reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
                      "input": 1.00, "cached": 0.05, "output": 5.00},
    "gpt-6.1-sol": {"reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
                    "input": 2.00, "cached": 0.10, "output": 10.00},
    "gpt-6-astra": {"reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
                   "input": 10.00, "cached": 1.00, "output": 50.00},
}


def openai_model_capabilities(model: str) -> dict:
    # Snapshots must be explicitly verified rather than guessed from prefixes.
    if model not in OPENAI_MODELS:
        raise ValueError("PROVIDER-023 此 Responses 模型尚無已驗證的圖片／Schema 能力，請選帳戶模型清單中的模型")
    return deepcopy(OPENAI_MODELS[model])


def official_openai_pricing() -> dict[str, dict[str, float]]:
    return {
        model: {
            "input_per_million": float(spec["input"]),
            "cached_input_per_million": float(spec["cached"]),
            "output_per_million": float(spec["output"]),
            "cache_write_per_million": float(spec["input"]) * 1.25,
            "batch_multiplier": 0.5,
            "long_context_threshold": 272_000,
        }
        for model, spec in OPENAI_MODELS.items()
    }
