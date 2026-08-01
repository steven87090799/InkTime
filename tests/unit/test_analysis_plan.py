from __future__ import annotations

from inktime.app.domain.analysis.plan import build_analysis_plan, canonical_json, fingerprint


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
    assert plan["low_vision_input"]["max_side"] == 512
    assert plan["high_vision_input"]["max_side"] == 1600
    assert "api_key" not in canonical_json(plan).casefold()
    assert fingerprint(plan) == fingerprint(_plan())
    assert fingerprint(plan) != fingerprint(_plan(prompt_version="prompt-2"))
    assert fingerprint(plan) != fingerprint(_plan(high_image_max_side=1024))
    assert plan["reasoning_effort"] == "none"
    assert fingerprint(plan) != fingerprint(_plan(reasoning_effort="low"))
