from __future__ import annotations

from inktime.app.domain.analysis.plan import (
    AI_IMAGE_JPEG_QUALITY,
    build_analysis_plan,
    canonical_json,
    fingerprint,
    normalize_analysis_plan,
)


def _plan(**changes):
    values = {
        "strategy": "smart_two_stage",
        "provider_route": [{"name": "primary", "priority": 1}],
        "low_model": "small",
        "high_model": "large",
        "stage_two_threshold": 65,
        "favorite_override": True,
        "scoring_profile": {
            "id": "rules-1",
            "memory_weight": 1,
            "beauty_weight": 2,
            "technical_weight": 3,
            "emotion_weight": 4,
            "favorite_bonus": 5,
        },
        "caption_controls": {"caption_variants_enabled": True},
        "prompt_version": "prompt-1",
        "high_image_max_side": 1600,
    }
    values.update(changes)
    return build_analysis_plan(**values)


def test_analysis_plan_is_canonical_non_secret_and_input_specific():
    plan = _plan()
    assert plan["strategy"] == "single"
    assert plan["analysis_call_policy"]["max_image_calls_per_photo"] == 1
    assert plan["vision_input"]["max_side"] == 1600
    assert "low_model" not in plan
    assert "high_model" not in plan
    assert "stage_two_threshold" not in plan
    assert "api_key" not in canonical_json(plan).casefold()
    assert fingerprint(plan) == fingerprint(_plan())
    assert fingerprint(plan) != fingerprint(_plan(prompt_version="prompt-2"))
    assert fingerprint(plan) != fingerprint(_plan(high_image_max_side=1024))
    assert plan["reasoning_effort"] == "none"
    assert fingerprint(plan) != fingerprint(_plan(reasoning_effort="low"))


def test_repair_policy_is_frozen_bounded_and_legacy_plans_are_upgraded():
    plan = _plan(repair_policy={"model": "repair-a", "max_tokens": 99999})
    assert plan["repair_policy"] == {
        "enabled": True,
        "model": "repair-a",
        "max_tokens": 1200,
        "max_attempts": 1,
        "text_only": True,
    }
    legacy = normalize_analysis_plan({"strategy": "single", "model": "vision-a"})
    assert legacy["repair_policy"]["model"] == "vision-a"
    assert legacy["repair_policy"]["max_attempts"] == 1
    assert legacy["repair_policy"]["text_only"] is True


def test_repair_policy_does_not_change_vision_input_contract():
    first = _plan(repair_policy={"model": "repair-a", "max_tokens": 256})
    second = _plan(repair_policy={"model": "repair-b", "max_tokens": 1200})
    assert first["vision_input"] == second["vision_input"]


def test_old_plan_pixel_version_upgrades_without_mutating_historical_plan():
    old = _plan()
    old["vision_input"]["preprocessing_version"] = "vision-input-v2"
    executed = normalize_analysis_plan(old)
    assert executed["vision_input"]["preprocessing_version"] == "vision-input-v3"
    assert old["vision_input"]["preprocessing_version"] == "vision-input-v2"
    assert fingerprint(old) != fingerprint(executed)


def test_default_production_vision_input_is_1024_high_jpeg88_and_single_call():
    plan = _plan(high_image_max_side=1024, caption_controls={"caption_variants_enabled": False})

    assert plan["vision_input"] == {
        "detail": "high",
        "max_side": 1024,
        "jpeg_quality": AI_IMAGE_JPEG_QUALITY,
        "exif_transpose": True,
        "preprocessing_version": "vision-input-v3",
    }
    assert AI_IMAGE_JPEG_QUALITY == 88
    assert plan["analysis_call_policy"] == {
        "max_image_calls_per_photo": 1,
        "repair_calls_are_text_only": True,
        "legacy_two_stage_replay": False,
    }
    assert plan["repair_policy"]["max_attempts"] == 1
    assert plan["repair_policy"]["text_only"] is True
