from __future__ import annotations

import json

import pytest

from inktime.app.domain.analysis.schema import AnalysisValidationError, validate_analysis_result


def valid_result(**updates):
    value = {
        "schema_version": 2,
        "caption": "家人在公園散步。",
        "types": ["人物", "日常"],
        "memory_score": 82,
        "beauty_score": 76,
        "technical_quality_score": 71,
        "emotion_score": 84,
        "side_caption": "風把這一天留得很輕。",
        "should_keep": True,
        "sensitive": False,
        "reason": "人物互動自然且清晰",
        "visual_orientation": {
            "rotation_cw": 0,
            "confidence": 1.0,
            "ambiguous": False,
            "evidence": ["faces_upright"],
        },
    }
    value.update(updates)
    return value


def test_strict_schema_accepts_expected_result():
    result = validate_analysis_result(json.dumps(valid_result(), ensure_ascii=False))
    assert result["memory_score"] == 82


def test_schema_v1_missing_orientation_is_safely_upgraded():
    legacy = valid_result(schema_version=1)
    legacy.pop("visual_orientation")
    assert validate_analysis_result(legacy)["visual_orientation"]["rotation_cw"] is None


@pytest.mark.parametrize(
    "orientation",
    [
        {"rotation_cw": False, "confidence": 1, "ambiguous": False, "evidence": ["faces_upright"]},
        {"rotation_cw": 0, "confidence": 1, "ambiguous": False, "evidence": []},
        {
            "rotation_cw": 0,
            "confidence": 1,
            "ambiguous": False,
            "evidence": ["faces_upright", "faces_upright"],
        },
        {"rotation_cw": 0, "confidence": 1, "ambiguous": False, "evidence": ["invalid"]},
        {"rotation_cw": None, "confidence": 0, "ambiguous": False, "evidence": ["insufficient_visual_cues"]},
    ],
)
def test_schema_v2_rejects_invalid_visual_orientation(orientation):
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(valid_result(visual_orientation=orientation))


@pytest.mark.parametrize(
    "value",
    [
        "```json\n{}\n```",
        "not-json",
        json.dumps(valid_result(memory_score=101), ensure_ascii=False),
        json.dumps(valid_result(types=["未允許類型"]), ensure_ascii=False),
        json.dumps(
            {key: value for key, value in valid_result().items() if key != "side_caption"}, ensure_ascii=False
        ),
        json.dumps(valid_result(extra="no"), ensure_ascii=False),
    ],
)
def test_strict_schema_rejects_invalid_output(value):
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(value)
