from __future__ import annotations

import json

from inktime.app.domain.analysis.schema import FULL_ANALYSIS_JSON_SCHEMA, json_schema_for_stage
from inktime.app.providers.openai_compatible import (
    ANALYSIS_USER_PROMPT,
    OpenAICompatibleProvider,
    analysis_system_prompt,
)
from inktime.app.repositories.settings import SETTING_DEFINITIONS
from inktime.app.services.analysis import FULL_ANALYSIS_TOKEN_CAP, PROMPT_VERSION, PhotoAnalysisService


def _controls(**updates):
    value = {
        "side_caption_min_chars": 8,
        "side_caption_max_chars": 16,
        "side_caption_custom_rules": "",
    }
    value.update(updates)
    return value


def test_only_minimal_side_caption_settings_are_registered():
    assert SETTING_DEFINITIONS["analysis.side_caption_min_chars"]["default"] == 8
    assert SETTING_DEFINITIONS["analysis.side_caption_max_chars"]["default"] == 16
    custom = SETTING_DEFINITIONS["analysis.side_caption_custom_rules"]
    assert custom["default"] == "" and custom["max_length"] == 1000 and custom["multiline"]
    removed = {
        "analysis.advanced_caption_enabled",
        "analysis.caption_variants_enabled",
        "analysis.caption_min_chars",
        "analysis.caption_target_chars",
        "analysis.caption_max_chars",
        "analysis.side_caption_target_chars",
        "analysis.copy_default_style",
        "analysis.copy_humor_level",
        "analysis.copy_poetic_level",
        "analysis.copy_banned_words",
        "analysis.copy_banned_patterns",
        "analysis.copy_custom_rules",
    }
    assert removed.isdisjoint(SETTING_DEFINITIONS)


def test_empty_custom_rule_adds_no_prompt_section_and_nonempty_adds_one_line():
    base = analysis_system_prompt("評分參考", _controls())
    custom = analysis_system_prompt(
        "評分參考", _controls(side_caption_custom_rules="避開雙關語\n保持口語")
    )
    assert "side_caption 自訂規則" not in base
    assert custom == base + "\n\nside_caption 自訂規則：避開雙關語 保持口語"


def test_compact_prompt_and_schema_have_no_advanced_caption_contract(tmp_path):
    controls = _controls()
    provider = OpenAICompatibleProvider(
        name="test", base_url="https://example.invalid", api_key="", caption_controls=controls
    )
    prompt = provider.system_prompt
    assert "輕微冷幽默" in prompt
    assert "不要描述畫面" in prompt
    assert "完整一句" in prompt
    for removed in (
        "caption target",
        "copy_humor_level",
        "copy_poetic_level",
        "special_codes",
        "people_count",
        "subject_position",
        "text_safe_area",
        "文案語感示例",
    ):
        assert removed not in prompt
    assert len(prompt) < 1600
    assert FULL_ANALYSIS_TOKEN_CAP == 512
    assert PhotoAnalysisService._prompt_version(None) == PROMPT_VERSION

    schema = json_schema_for_stage("single", caption_controls=controls)
    assert schema == FULL_ANALYSIS_JSON_SCHEMA
    assert set(schema["schema"]["properties"]) == {
        "schema_version",
        "types",
        "memory_score",
        "visual_score",
        "special_level",
        "side_caption",
        "content_filter",
        "visual_orientation",
    }

    image = tmp_path / "thumbnail.jpg"
    image.write_bytes(b"thumbnail")
    body = provider.build_analysis_request_body(
        image_path=image,
        model="vision",
        detail="high",
        stage="single",
        reasoning_effort="none",
    )
    user_content = body["messages"][1]["content"]
    assert user_content[0] == {"type": "text", "text": ANALYSIS_USER_PROMPT}
    assert len([item for item in user_content if item["type"] == "image_url"]) == 1
    assert provider.last_request_metrics["prompt_chars"] == len(prompt)
    assert provider.last_request_metrics["prompt_chars"] < 1600
    assert "caption" not in json.dumps(schema, ensure_ascii=False).replace("side_caption", "")
    provider.close()


def test_side_caption_bounds_are_clamped_to_eight_through_sixteen():
    schema = json_schema_for_stage(
        "single",
        caption_controls=_controls(side_caption_min_chars=0, side_caption_max_chars=200),
    )
    assert schema["schema"]["properties"]["side_caption"] == {
        "type": "string",
        "minLength": 8,
        "maxLength": 16,
    }
