"""Frozen, non-sensitive analysis plans and stable SHA-256 fingerprints."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


VISION_INPUT_VERSION = "vision-input-v2"
SCHEMA_VERSION = 2


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_analysis_plan(*, strategy: str, provider_route: Sequence[Mapping[str, Any]], low_model: str,
                        high_model: str, stage_two_threshold: float, favorite_override: bool,
                        scoring_profile: Mapping[str, Any], caption_controls: Mapping[str, Any] | None,
                        prompt_version: str, high_image_max_side: int) -> dict[str, Any]:
    """Return a complete immutable plan without secrets or endpoint URLs."""
    high_side = int(high_image_max_side)
    if high_side not in {1024, 1600}:
        high_side = 1024
    route = [{"name": str(item.get("name", "")), "priority": int(item.get("priority", 100))}
             for item in provider_route]
    return {
        "strategy": str(strategy), "provider_route": route, "low_model": str(low_model),
        "high_model": str(high_model), "stage_two_threshold": float(stage_two_threshold),
        "favorite_override": bool(favorite_override), "scoring_profile_id": str(scoring_profile.get("id", "")),
        "ranking_weights": {"memory": float(scoring_profile.get("memory_weight", 0)),
                            "beauty": float(scoring_profile.get("beauty_weight", 0)),
                            "technical_quality": float(scoring_profile.get("technical_weight", 0)),
                            "emotion": float(scoring_profile.get("emotion_weight", 0))},
        "favorite_bonus": float(scoring_profile.get("favorite_bonus", 0)),
        "caption_controls": dict(caption_controls or {}), "prompt_version": str(prompt_version),
        "schema_version": SCHEMA_VERSION, "schema_kind": {"low": "basic", "high": "full"},
        "low_vision_input": {"detail": "low", "max_side": 512, "jpeg_quality": 88,
                              "exif_transpose": True, "preprocessing_version": VISION_INPUT_VERSION},
        "high_vision_input": {"detail": "high", "max_side": high_side, "jpeg_quality": 88,
                               "exif_transpose": True, "preprocessing_version": VISION_INPUT_VERSION},
    }
