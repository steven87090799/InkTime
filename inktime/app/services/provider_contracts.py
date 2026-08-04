"""Bounded Provider capability contracts using only synthetic image data."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from inktime.app.domain.analysis import REPAIR_TOKEN_CAP
from inktime.app.domain.analysis.schema import AnalysisValidationError, validate_analysis_result
from inktime.app.providers.base import ProviderResponse, VisionAttemptState


CONTRACT_LEVELS = (1, 2, 3)
LEVEL2_STAGE = "provider_contract_level2"


def _synthetic_contract_image(path: Path) -> Path:
    """Create the deterministic fixture used by Level 2 and Level 3 only."""

    image = Image.new("RGB", (256, 256), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 32, 116, 178), fill=(220, 40, 40), outline=(100, 0, 0), width=3)
    draw.ellipse((138, 42, 226, 130), fill=(30, 80, 220), outline=(0, 20, 100), width=3)
    try:
        font = ImageFont.load_default()
    except (OSError, ValueError):
        font = None
    draw.text((34, 208), "INKTIME TEST", fill=(0, 0, 0), font=font)
    image.save(path, format="PNG", optimize=False)
    return path


def _usage_snapshot(provider: Any, model: str, response: ProviderResponse) -> dict[str, Any]:
    usage = response.usage
    reported = usage.provider_reported_cost
    usage_missing = (
        usage.input_tokens <= 0
        and usage.output_tokens <= 0
        and usage.cached_tokens <= 0
        and usage.cache_write_tokens <= 0
        and usage.reasoning_tokens <= 0
        and reported is None
    )
    estimated = None if usage_missing else provider.estimate_cost(model, usage)
    if reported is not None:
        source = "provider_reported"
    elif estimated is not None:
        source = "estimated"
    else:
        source = "unknown"
    return {
        "input_tokens": max(0, int(usage.input_tokens)),
        "output_tokens": max(0, int(usage.output_tokens)),
        "cached_tokens": max(0, int(usage.cached_tokens)),
        "reasoning_tokens": max(0, int(usage.reasoning_tokens)),
        "cost_source": source,
        "provider_reported_cost": reported,
        "estimated_cost": estimated,
        "unknown_cost_count": int(source == "unknown"),
    }


def _privacy_policy_snapshot(provider: Any) -> dict[str, Any]:
    if str(getattr(provider, "kind", "")).casefold() != "openrouter":
        return {
            "data_collection": "not_applicable",
            "zdr": False,
            "configured": False,
        }
    options = getattr(provider, "options", {})
    options = options if isinstance(options, dict) else {}
    data_collection = options.get("data_collection")
    if data_collection not in {"allow", "deny"}:
        data_collection = "openrouter_default"
    zdr_explicit = type(options.get("zdr")) is bool
    zdr = bool(options.get("zdr")) if zdr_explicit else False
    return {
        "data_collection": data_collection,
        "zdr": zdr,
        "configured": data_collection == "deny" and zdr_explicit and zdr,
    }


def _checks(provider: Any, *, level: int, ok: bool, schema_valid: bool | None, usage: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "connectivity": "pass" if (level == 1 and ok) or level >= 2 and usage is not None else "fail",
        "vision": "not_run" if level == 1 else "pass" if usage is not None else "fail",
        "json_schema": "not_run" if level < 2 else "pass" if schema_valid else "fail",
        "usage": "not_run" if usage is None else "pass",
        "cost_source": usage.get("cost_source") if usage is not None else None,
        "privacy_policy": _privacy_policy_snapshot(provider),
    }


def _safe_failure(
    provider: Any,
    level: int,
    message: str,
    *,
    vision_requests: int = 0,
    repair_requests: int = 0,
    repair_attempts: int = 0,
    repair_responses: int = 0,
    network_request_attempts: int = 0,
    network_responses: int = 0,
    vision_started: bool = False,
    vision_completed: bool = False,
    repair_attempted: bool = False,
    repair_completed: bool = False,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "level": level,
        "ok": False,
        "message": message,
        "vision_requests": vision_requests,
        "repair_requests": repair_requests,
        "repair_attempts": repair_attempts,
        "repair_responses": repair_responses,
        "network_request_attempts": network_request_attempts,
        "network_responses": network_responses,
        "network_requests": network_request_attempts,
        "request_counting_policy": "conservative_attempted_calls",
        "vision_started": vision_started,
        "vision_completed": vision_completed,
        "repair_attempted": repair_attempted,
        "repair_completed": repair_completed,
        "schema_valid": False,
        "usage": usage,
    }
    result["checks"] = _checks(provider, level=level, ok=False, schema_valid=False, usage=usage)
    return result


def _valid_level2_response(content: str) -> bool:
    try:
        value = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("vision_ok") is True
        and isinstance(value.get("detected_shapes"), list)
        and len(value["detected_shapes"]) == 2
        and all(isinstance(item, str) for item in value["detected_shapes"])
        and set(value["detected_shapes"]) == {"rectangle", "circle"}
    )


def run_provider_contract(provider: Any, *, level: int, model: str) -> dict[str, Any]:
    """Run one explicitly selected contract without touching production photos.

    Level 1 performs only the bounded `/models` connection check.  Level 2
    sends one deterministic 256px synthetic image with a small output cap and
    never repairs.  Level 3 sends one synthetic image using the full schema and
    permits at most one text-only repair when validation fails.
    """

    if level not in CONTRACT_LEVELS:
        raise ValueError("Provider contract level 只允許 1、2 或 3")
    if level == 1:
        try:
            valid, _ = provider.validate_config()
        except Exception as exc:  # Keep transport details out of the API result.
            return _safe_failure(
                provider,
                level,
                f"Level 1 connection failed: {exc.__class__.__name__}",
                network_request_attempts=1,
                network_responses=0,
            )
        result: dict[str, Any] = {
            "level": level,
            "ok": bool(valid),
            "message": "Level 1 connection and model capability passed" if valid else "Level 1 connection failed",
            "vision_requests": 0,
            "repair_requests": 0,
            "repair_attempts": 0,
            "repair_responses": 0,
            "network_request_attempts": 1,
            "network_responses": 1 if valid else 0,
            "network_requests": 1,
            "request_counting_policy": "conservative_attempted_calls",
            "vision_started": False,
            "vision_completed": False,
            "repair_attempted": False,
            "repair_completed": False,
            "schema_valid": None,
            "usage": None,
        }
        result["checks"] = _checks(provider, level=level, ok=bool(valid), schema_valid=None, usage=None)
        return result

    vision_requests = 0
    repair_requests = 0
    repair_attempts = 0
    repair_responses = 0
    network_request_attempts = 0
    network_responses = 0
    vision_started = False
    vision_completed = False
    repair_attempted = False
    repair_completed = False
    with tempfile.TemporaryDirectory(prefix="inktime-provider-contract-") as directory:
        image_path = _synthetic_contract_image(Path(directory) / "inktime-test.png")
        vision_requests = 1
        network_request_attempts = 1
        vision_started = True
        try:
            response = provider.analyze(
                image_path=image_path,
                model=model,
                detail="high",
                stage=LEVEL2_STAGE if level == 2 else "single",
                max_tokens=256 if level == 2 else 2048,
                reasoning_effort="none",
                vision_attempt=VisionAttemptState(),
            )
            network_responses = 1
            vision_completed = True
        except Exception as exc:
            return _safe_failure(
                provider,
                level,
                f"Level {level} synthetic Vision failed: {exc.__class__.__name__}",
                vision_requests=vision_requests,
                network_request_attempts=network_request_attempts,
                network_responses=network_responses,
                vision_started=vision_started,
                vision_completed=vision_completed,
            )

        usage = _usage_snapshot(provider, model, response)
        schema_valid = False
        if level == 2:
            schema_valid = _valid_level2_response(response.content)
            if not schema_valid:
                result = {
                    "level": level,
                    "ok": False,
                    "message": "Level 2 response did not satisfy the synthetic shape contract; no repair was attempted",
                    "vision_requests": vision_requests,
                    "repair_requests": repair_requests,
                    "repair_attempts": repair_attempts,
                    "repair_responses": repair_responses,
                    "network_request_attempts": network_request_attempts,
                    "network_responses": network_responses,
                    "network_requests": network_request_attempts,
                    "request_counting_policy": "conservative_attempted_calls",
                    "vision_started": vision_started,
                    "vision_completed": vision_completed,
                    "repair_attempted": repair_attempted,
                    "repair_completed": repair_completed,
                    "schema_valid": False,
                    "usage": usage,
                }
                result["checks"] = _checks(
                    provider, level=level, ok=False, schema_valid=False, usage=usage
                )
                return result
        else:
            try:
                validate_analysis_result(response.content)
                schema_valid = True
            except AnalysisValidationError:
                pass
        if not schema_valid and level == 3:
            repair_attempts = 1
            repair_requests = 1
            repair_attempted = True
            network_request_attempts += 1
            try:
                repaired = provider.repair_json(
                    invalid_content=response.content,
                    validation_error="synthetic contract schema validation failed",
                    model=model,
                    max_tokens=REPAIR_TOKEN_CAP,
                    stage="single",
                )
                repair_responses = 1
                repair_completed = True
                network_responses += 1
                repair_usage = _usage_snapshot(provider, model, repaired)
                # Preserve the fact that a repair happened while exposing only
                # bounded usage fields, never raw provider response content.
                for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
                    usage[key] += repair_usage[key]
                usage["unknown_cost_count"] += repair_usage["unknown_cost_count"]
                if repair_usage["provider_reported_cost"] is not None:
                    usage["provider_reported_cost"] = (
                        float(usage["provider_reported_cost"] or 0)
                        + float(repair_usage["provider_reported_cost"])
                    )
                if repair_usage["estimated_cost"] is not None:
                    usage["estimated_cost"] = float(usage["estimated_cost"] or 0) + float(
                        repair_usage["estimated_cost"]
                    )
                usage["cost_source"] = (
                    "unknown"
                    if usage["unknown_cost_count"]
                    else "provider_reported"
                    if usage["provider_reported_cost"] is not None
                    else "estimated"
                    if usage["estimated_cost"] is not None
                    else "unknown"
                )
                validate_analysis_result(repaired.content)
                schema_valid = True
            except AnalysisValidationError:
                schema_valid = False
            except Exception:
                if usage is not None:
                    usage["unknown_cost_count"] += 1
                    usage["cost_source"] = "unknown"
                return _safe_failure(
                    provider,
                    level,
                    "Level 3 text-only repair failed",
                    vision_requests=vision_requests,
                    repair_requests=repair_requests,
                    repair_attempts=repair_attempts,
                    repair_responses=repair_responses,
                    network_request_attempts=network_request_attempts,
                    network_responses=network_responses,
                    vision_started=vision_started,
                    vision_completed=vision_completed,
                    repair_attempted=repair_attempted,
                    repair_completed=repair_completed,
                    usage=usage,
                )

        result = {
            "level": level,
            "ok": schema_valid,
            "message": f"Level {level} synthetic contract {'passed' if schema_valid else 'failed'}",
            "vision_requests": vision_requests,
            "repair_requests": repair_requests,
            "repair_attempts": repair_attempts,
            "repair_responses": repair_responses,
            "network_request_attempts": network_request_attempts,
            "network_responses": network_responses,
            "network_requests": network_request_attempts,
            "request_counting_policy": "conservative_attempted_calls",
            "vision_started": vision_started,
            "vision_completed": vision_completed,
            "repair_attempted": repair_attempted,
            "repair_completed": repair_completed,
            "schema_valid": schema_valid,
            "usage": usage,
        }
        result["checks"] = _checks(
            provider, level=level, ok=schema_valid, schema_valid=schema_valid, usage=usage
        )
        return result
