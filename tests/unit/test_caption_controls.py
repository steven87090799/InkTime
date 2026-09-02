from __future__ import annotations

import json

from inktime.app.domain.analysis.schema import (
    FULL_ANALYSIS_JSON_SCHEMA,
    json_schema_for_stage,
)
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.repositories.settings import SETTING_DEFINITIONS
from inktime.app.services.analysis import PROMPT_VERSION, PhotoAnalysisService


def _controls(**updates):
    value = {
        "caption_min_chars": 120,
        "caption_target_chars": 160,
        "caption_max_chars": 200,
        "side_caption_min_chars": 8,
        "side_caption_target_chars": 12,
        "side_caption_max_chars": 16,
        "copy_default_style": "literary",
        "copy_humor_level": 1,
        "copy_poetic_level": 2,
        "copy_avoid_cliche": True,
        "copy_avoid_direct_description": True,
        "copy_forbid_exclamation": True,
        "copy_forbid_like_phrase": True,
        "copy_max_commas": 2,
        "copy_avoid_abstract_ending": True,
        "copy_banned_words": ["世界"],
        "copy_banned_patterns": ["模板句"],
        "copy_custom_rules": "",
        "caption_variants_enabled": False,
    }
    value.update(updates)
    return value


def test_caption_feature_defaults_enable_one_literary_caption():
    assert SETTING_DEFINITIONS["analysis.advanced_caption_enabled"]["default"] is True
    assert SETTING_DEFINITIONS["analysis.caption_variants_enabled"]["default"] is False
    assert SETTING_DEFINITIONS["analysis.copy_default_style"]["default"] == "literary"
    assert SETTING_DEFINITIONS["analysis.copy_poetic_level"]["default"] == 2
    assert SETTING_DEFINITIONS["render.caption_wrap_enabled"]["default"] is False
    assert json_schema_for_stage("single_high") == FULL_ANALYSIS_JSON_SCHEMA
    assert PhotoAnalysisService._prompt_version(None) == PROMPT_VERSION


def test_advanced_schema_and_prompt_keep_legacy_variant_compatibility():
    controls = _controls(caption_variants_enabled=True)
    schema = json_schema_for_stage("single_high", caption_controls=controls)
    assert schema["schema"]["properties"]["caption"]["minLength"] == 100
    assert "details" not in schema["schema"]["properties"]
    prompt = OpenAICompatibleProvider(
        name="test", base_url="https://example.invalid", api_key="", caption_controls=controls
    ).system_prompt
    assert "繁體中文" in prompt and "嚴禁簡體字" in prompt
    assert "世界" in prompt


def test_single_caption_prompt_is_literary_compact_and_omits_empty_fields(tmp_path):
    controls = _controls(copy_banned_words=[], copy_banned_patterns=[], copy_custom_rules="")
    provider = OpenAICompatibleProvider(
        name="test", base_url="https://example.invalid", api_key="", caption_controls=controls
    )
    prompt = provider.system_prompt
    assert "不需要為了湊字數" in prompt
    assert "不可確認內容" in prompt
    assert "不虛構" in prompt
    assert "禁止詞：無" not in prompt
    assert "禁止句型：無" not in prompt
    assert "自訂規則：無" not in prompt
    assert "不要求多風格候選" not in prompt
    assert "caption_variants" not in json.dumps(
        json_schema_for_stage("single", caption_controls=controls), ensure_ascii=False
    )

    image = tmp_path / "thumbnail.jpg"
    image.write_bytes(b"thumbnail")
    body = provider.build_analysis_request_body(
        image_path=image,
        model="vision",
        detail="high",
        stage="single",
        reasoning_effort="none",
    )
    assert body["messages"][1]["content"][0]["text"] == "分析這張照片。"
    assert provider.last_request_metrics["prompt_chars"] == len(prompt)
    assert provider.last_request_metrics["prompt_chars"] < 2000
    assert provider.last_request_metrics["schema_chars"] <= 6634


def test_legacy_caption_limits_are_normalized_before_schema_generation():
    controls = _controls(
        caption_max_chars=220,
        side_caption_min_chars=7,
        side_caption_target_chars=20,
        side_caption_max_chars=42,
    )
    schema = json_schema_for_stage("single", caption_controls=controls)
    properties = schema["schema"]["properties"]
    assert properties["caption"]["maxLength"] == 100
    assert properties["side_caption"] == {
        "type": "string",
        "minLength": 8,
        "maxLength": 16,
    }


def test_caption_settings_change_cache_fingerprint_and_legacy_variant_fallback():
    controls = _controls()
    assert PhotoAnalysisService._prompt_version(controls) != PhotoAnalysisService._prompt_version(
        _controls(copy_poetic_level=1)
    )
    assert PhotoAnalysisService._prompt_version(controls) != PhotoAnalysisService._prompt_version(
        _controls(copy_default_style="natural")
    )
    result = {"side_caption": "既有短句", "details": {"caption_variants": {"natural": "自然短句"}}}
    selected = PhotoAnalysisService._apply_caption_variant(
        result,
        _controls(copy_default_style="literary", caption_variants_enabled=True),
    )
    assert selected["side_caption"] == "既有短句"
