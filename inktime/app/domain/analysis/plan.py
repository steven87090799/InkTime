"""Frozen, non-sensitive analysis plans and stable SHA-256 fingerprints."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


VISION_INPUT_VERSION = "vision-input-v2"
PROVIDER_PROMPT_CONTRACT_VERSION = "provider-prompt-contract-v1"
AI_IMAGE_JPEG_QUALITY = 88
SCHEMA_VERSION = 4
REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
SINGLE_ANALYSIS_STRATEGIES = {"single", "single_high", "high_quality"}
LEGACY_ANALYSIS_STRATEGIES = {"custom", "low_cost", "smart", "smart_two_stage"}
REPAIR_TOKEN_CAP = 1200


def normalize_analysis_strategy(value: Any) -> str:
    """Resolve every cloud strategy to the one-image execution contract."""
    resolved = str(value or "single").strip().casefold()
    if resolved in SINGLE_ANALYSIS_STRATEGIES:
        return "single"
    if resolved in LEGACY_ANALYSIS_STRATEGIES:
        # The old names remain readable at API/job boundaries, but they must not
        # re-enable the removed low-cost -> high-quality image sequence.
        return "single"
    if resolved == "local":
        return resolved
    raise ValueError("不支援的分析策略")


def normalize_reasoning_effort(value: Any) -> str:
    resolved = str(value or "none").strip().casefold()
    if resolved not in REASONING_EFFORTS:
        raise ValueError(f"reasoning_effort 只允許：{'、'.join(REASONING_EFFORTS)}")
    return resolved


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def provider_prompt_contract_sha256(
    *,
    prompt_version: str,
    scoring_rules_sha256: str,
    schema_version: int,
    schema_kind: str,
    caption_generation_controls: Mapping[str, Any] | None,
    reasoning_effort: str,
    provider_behavior_revision: str,
) -> str:
    """Hash only behavior that can change the provider's Vision JSON contract."""

    return fingerprint(
        {
            "provider_prompt_contract_version": PROVIDER_PROMPT_CONTRACT_VERSION,
            "prompt_version": str(prompt_version),
            "scoring_rules_sha256": str(scoring_rules_sha256),
            "schema_version": int(schema_version),
            "schema_kind": str(schema_kind),
            "caption_generation_controls": dict(caption_generation_controls or {}),
            "reasoning_effort": normalize_reasoning_effort(reasoning_effort),
            "provider_behavior_revision": str(provider_behavior_revision),
        }
    )


def build_analysis_plan(
    *,
    strategy: str,
    provider_route: Sequence[Mapping[str, Any]],
    low_model: str,
    high_model: str,
    stage_two_threshold: float,
    favorite_override: bool,
    scoring_profile: Mapping[str, Any],
    caption_controls: Mapping[str, Any] | None,
    prompt_version: str,
    high_image_max_side: int,
    caption_display_controls: Mapping[str, Any] | None = None,
    prefilter: Mapping[str, Any] | None = None,
    execution_policy: Mapping[str, Any] | None = None,
    scoring_rules: str = "",
    reasoning_effort: str = "none",
    repair_policy: Mapping[str, Any] | None = None,
    provider_behavior_revision: str = "",
) -> dict[str, Any]:
    """Return a complete immutable single-image plan.

    The legacy low/high arguments remain in the function signature so callers
    that still construct a plan can be upgraded without a flag day.  New
    frozen plans contain only the canonical single-call fields; historical
    frozen plans are upgraded by :func:`normalize_analysis_plan`.
    """
    normalized_strategy = normalize_analysis_strategy(strategy)
    high_side = int(high_image_max_side)
    if high_side not in {512, 1024, 1600}:
        high_side = 1024
    route = []
    for item in provider_route:
        route_item = {
            "provider_id": str(item.get("provider_id") or item.get("id") or ""),
            "display_name": str(item.get("display_name") or item.get("name") or ""),
            "priority": int(item.get("priority", 100)),
            "config_revision": str(item.get("config_revision") or item.get("updated_at") or ""),
        }
        configured_model = str(item.get("model") or "").strip()
        # Keep the historical plan shape/fingerprint unchanged when a
        # Provider intentionally uses the global model fallback.
        if configured_model:
            route_item["model"] = configured_model
        route.append(route_item)
    rules_sha256 = hashlib.sha256(str(scoring_rules).encode("utf-8")).hexdigest()
    high_input = {
        "detail": "high",
        "max_side": high_side,
        "jpeg_quality": AI_IMAGE_JPEG_QUALITY,
        "exif_transpose": True,
        "preprocessing_version": VISION_INPUT_VERSION,
    }
    configured_repair = dict(repair_policy or {})
    repair_model = str(configured_repair.get("model") or high_model).strip()
    try:
        configured_repair_tokens = int(configured_repair.get("max_tokens", REPAIR_TOKEN_CAP))
    except (TypeError, ValueError):
        configured_repair_tokens = REPAIR_TOKEN_CAP
    repair_max_tokens = max(256, min(REPAIR_TOKEN_CAP, configured_repair_tokens))
    schema_kind = "basic" if normalized_strategy == "local" else "full"
    normalized_effort = normalize_reasoning_effort(reasoning_effort)
    behavior_revision = str(provider_behavior_revision or (route[0]["config_revision"] if route else ""))
    contract_sha256 = provider_prompt_contract_sha256(
        prompt_version=str(prompt_version),
        scoring_rules_sha256=rules_sha256,
        schema_version=SCHEMA_VERSION,
        schema_kind=schema_kind,
        caption_generation_controls=caption_controls,
        reasoning_effort=normalized_effort,
        provider_behavior_revision=behavior_revision,
    )
    return {
        "strategy": normalized_strategy,
        "model": str(high_model),
        "provider_route": route,
        "favorite_override": bool(favorite_override),
        "scoring_profile_id": str(scoring_profile.get("id", "")),
        "ranking_weights": {"memory": 50.0, "visual": 25.0, "local_quality": 25.0},
        "favorite_bonus": 1,
        "scoring_rules_sha256": rules_sha256,
        "scoring_rules": str(scoring_rules),
        "provider_behavior_revision": behavior_revision,
        "provider_prompt_contract_version": PROVIDER_PROMPT_CONTRACT_VERSION,
        "provider_prompt_contract_sha256": contract_sha256,
        "caption_controls": dict(caption_controls or {}),
        "caption_display_controls": dict(caption_display_controls or {}),
        "prompt_version": str(prompt_version),
        "schema_version": SCHEMA_VERSION,
        "schema_kind": schema_kind,
        "analysis_call_policy": {
            "max_image_calls_per_photo": 1,
            "repair_calls_are_text_only": True,
            "legacy_two_stage_replay": False,
        },
        "vision_input": high_input,
        "prefilter": dict(prefilter or {}),
        "ai_execution_policy": dict(execution_policy or {}),
        "reasoning_effort": normalized_effort,
        "repair_policy": {
            "enabled": bool(configured_repair.get("enabled", True)),
            "model": repair_model,
            "max_tokens": repair_max_tokens,
            "max_attempts": 1,
            "text_only": True,
        },
    }


def normalize_analysis_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade a frozen legacy plan to the canonical one-image shape."""
    raw = dict(plan)
    strategy = normalize_analysis_strategy(raw.get("strategy", "single"))
    if "model" not in raw:
        raw["model"] = str(raw.get("high_model") or raw.get("low_model") or "")
    if not str(raw.get("model") or "").strip() and strategy != "local":
        raise ValueError("分析計畫缺少 model")
    if "vision_input" not in raw:
        raw["vision_input"] = dict(
            raw.get("high_vision_input")
            or raw.get("low_vision_input")
            or {
                "detail": "high",
                "max_side": 1024,
                "jpeg_quality": AI_IMAGE_JPEG_QUALITY,
                "exif_transpose": True,
                "preprocessing_version": VISION_INPUT_VERSION,
            }
        )
    vision = dict(raw["vision_input"])
    max_side = int(vision.get("max_side", 1024))
    if max_side not in {512, 1024, 1600}:
        max_side = 1024
    vision.update(
        detail=str(vision.get("detail", "high")),
        max_side=max_side,
        jpeg_quality=int(vision.get("jpeg_quality", AI_IMAGE_JPEG_QUALITY)),
        exif_transpose=bool(vision.get("exif_transpose", True)),
        preprocessing_version=str(vision.get("preprocessing_version", VISION_INPUT_VERSION)),
    )
    raw["vision_input"] = vision
    raw_controls = dict(raw.get("caption_controls") or {})
    display_controls = dict(raw.get("caption_display_controls") or {})
    if "copy_default_style" in raw_controls:
        display_controls.setdefault("copy_default_style", raw_controls["copy_default_style"])
    raw["caption_controls"] = raw_controls
    raw["caption_display_controls"] = display_controls
    raw["strategy"] = strategy
    raw["schema_kind"] = "basic" if strategy == "local" else "full"
    raw["schema_version"] = SCHEMA_VERSION
    has_scoring_contract = "scoring_rules" in raw or "scoring_rules_sha256" in raw
    if has_scoring_contract:
        scoring_rules = str(raw.get("scoring_rules", ""))
        raw["scoring_rules_sha256"] = str(
            raw.get("scoring_rules_sha256") or hashlib.sha256(scoring_rules.encode("utf-8")).hexdigest()
        )
        route = list(raw.get("provider_route") or [])
        raw["provider_behavior_revision"] = str(
            raw.get("provider_behavior_revision")
            or (route[0].get("config_revision") if route and isinstance(route[0], Mapping) else "")
            or ""
        )
        raw["provider_prompt_contract_version"] = PROVIDER_PROMPT_CONTRACT_VERSION
        raw["provider_prompt_contract_sha256"] = provider_prompt_contract_sha256(
            prompt_version=str(raw.get("prompt_version", "")),
            scoring_rules_sha256=raw["scoring_rules_sha256"],
            schema_version=int(raw["schema_version"]),
            schema_kind=str(raw["schema_kind"]),
            caption_generation_controls=raw_controls,
            reasoning_effort=normalize_reasoning_effort(raw.get("reasoning_effort", "none")),
            provider_behavior_revision=raw["provider_behavior_revision"],
        )
    policy = dict(raw.get("analysis_call_policy") or {})
    policy.update(max_image_calls_per_photo=1, repair_calls_are_text_only=True, legacy_two_stage_replay=False)
    raw["analysis_call_policy"] = policy
    configured_repair = dict(raw.get("repair_policy") or {})
    legacy_repair_model = str(
        configured_repair.get("model")
        or raw.get("repair_model")
        or raw.get("model")
        or raw.get("high_model")
        or raw.get("low_model")
        or ""
    ).strip()
    try:
        configured_repair_tokens = int(configured_repair.get("max_tokens", REPAIR_TOKEN_CAP))
    except (TypeError, ValueError):
        configured_repair_tokens = REPAIR_TOKEN_CAP
    raw["repair_policy"] = {
        "enabled": bool(configured_repair.get("enabled", True)),
        "model": legacy_repair_model,
        "max_tokens": max(256, min(REPAIR_TOKEN_CAP, configured_repair_tokens)),
        "max_attempts": 1,
        "text_only": True,
    }
    return raw
