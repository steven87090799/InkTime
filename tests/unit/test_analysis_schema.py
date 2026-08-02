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


def test_schema_v3_normalizes_grades_and_preserves_confidence_details():
    result = valid_result(
        schema_version=3,
        memory_grade="A",
        aesthetic_grade="B",
        technical_grade="S",
        emotion_grade="C",
        display_suitability_grade="A",
        confidence={"overall": 0.91, "orientation": 0.72},
    )
    for field in ("memory_score", "beauty_score", "technical_quality_score", "emotion_score"):
        result.pop(field)

    normalized = validate_analysis_result(result)

    assert normalized["memory_score"] == 85.0
    assert normalized["beauty_score"] == 75.0
    assert normalized["technical_quality_score"] == 95.0
    assert normalized["emotion_score"] == 60.0
    assert normalized["details"]["display_suitability_grade"] == "A"
    assert normalized["details"]["confidence"]["overall"] == 0.91


def test_schema_v3_rejects_unknown_grade():
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(valid_result(schema_version=3, memory_grade="Z"))


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
