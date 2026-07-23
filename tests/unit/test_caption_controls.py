from __future__ import annotations

from inktime.app.domain.analysis.schema import (
    FULL_ANALYSIS_JSON_SCHEMA,
    json_schema_for_stage,
)
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.repositories.settings import SETTING_DEFINITIONS
from inktime.app.services.analysis import PhotoAnalysisService, PROMPT_VERSION


def _controls(**updates):
    value = {
        "caption_min_chars": 120, "caption_target_chars": 160, "caption_max_chars": 220,
        "side_caption_min_chars": 10, "side_caption_target_chars": 22, "side_caption_max_chars": 42,
        "copy_default_style": "natural", "copy_humor_level": 1, "copy_poetic_level": 1,
        "copy_avoid_cliche": True, "copy_avoid_direct_description": True,
        "copy_forbid_exclamation": True, "copy_forbid_like_phrase": True,
        "copy_max_commas": 2, "copy_avoid_abstract_ending": True,
        "copy_banned_words": ["世界"], "copy_banned_patterns": ["模板句"],
        "copy_custom_rules": "", "caption_variants_enabled": True,
    }
    value.update(updates)
    return value


def test_caption_feature_defaults_preserve_existing_behavior():
    assert SETTING_DEFINITIONS["analysis.advanced_caption_enabled"]["default"] is False
    assert SETTING_DEFINITIONS["analysis.caption_variants_enabled"]["default"] is False
    assert SETTING_DEFINITIONS["render.caption_wrap_enabled"]["default"] is False
    assert json_schema_for_stage("single_high") is FULL_ANALYSIS_JSON_SCHEMA
    assert PhotoAnalysisService._prompt_version(None) == PROMPT_VERSION


def test_advanced_schema_and_prompt_include_variant_rules():
    controls = _controls()
    schema = json_schema_for_stage("single_high", caption_controls=controls)
    assert schema["schema"]["properties"]["caption"]["minLength"] == 120
    assert set(schema["schema"]["properties"]["details"]["properties"]["caption_variants"]["properties"]) == {
        "natural", "warm", "literary", "humorous", "minimal"
    }
    prompt = OpenAICompatibleProvider(name="test", base_url="https://example.invalid", api_key="", caption_controls=controls).system_prompt
    assert "繁體中文" in prompt and "像是、彷彿、彷佛" in prompt and "世界" in prompt


def test_caption_settings_change_the_cache_fingerprint_and_variant_fallback():
    controls = _controls()
    assert PhotoAnalysisService._prompt_version(controls) != PhotoAnalysisService._prompt_version(_controls(copy_poetic_level=2))
    result = {"side_caption": "既有短句", "details": {"caption_variants": {"natural": "自然短句"}}}
    selected = PhotoAnalysisService._apply_caption_variant(result, _controls(copy_default_style="literary"))
    assert selected["side_caption"] == "自然短句"
