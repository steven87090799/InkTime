from __future__ import annotations

import json

import pytest

from inktime.app.domain.analysis.schema import (
    AnalysisValidationError,
    FULL_ANALYSIS_JSON_SCHEMA,
    validate_analysis_result,
)


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
    assert normalized["beauty_score"] == 70.0
    assert normalized["technical_quality_score"] == 95.0
    assert normalized["emotion_score"] == 55.0
    assert normalized["details"]["display_suitability_grade"] == "A"
    assert normalized["details"]["confidence"]["overall"] == 0.91


def test_schema_v3_rejects_unknown_grade():
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(valid_result(schema_version=3, memory_grade="Z"))


def test_schema_v3_requires_all_grades_and_confidence_without_silent_unknown_fill():
    value = valid_result(schema_version=3, memory_grade="A", beauty_grade="B", technical_grade="C")
    value.pop("emotion_score")
    with pytest.raises(AnalysisValidationError, match="emotion_grade"):
        validate_analysis_result(value)

    details_only = valid_result(
        schema_version=3,
        details={
            "memory_grade": "A",
            "beauty_grade": "B",
            "technical_grade": "C",
            "emotion_grade": "D",
            "confidence": 0.8,
        },
    )
    for field in ("memory_score", "beauty_score", "technical_quality_score", "emotion_score"):
        details_only.pop(field)
    normalized = validate_analysis_result(details_only)
    assert normalized["emotion_score"] == 35.0

    missing_confidence = dict(details_only)
    missing_confidence["details"] = dict(details_only["details"])
    missing_confidence["details"].pop("confidence")
    with pytest.raises(AnalysisValidationError, match="confidence"):
        validate_analysis_result(missing_confidence)


def _v3_result(**updates):
    value = valid_result(
        schema_version=3,
        details={
            "memory_grade": "A",
            "beauty_grade": "B",
            "technical_grade": "C",
            "emotion_grade": "D",
            "confidence": 0.8,
        },
    )
    value.update(updates)
    return value


def test_schema_v3_normalized_cache_is_readable_and_explicit_unknown_is_zero():
    normalized = validate_analysis_result(
        _v3_result(
            details={
                "memory_grade": "unknown",
                "beauty_grade": "B",
                "technical_grade": "C",
                "emotion_grade": "D",
                "confidence": {"overall": 0.0},
            }
        )
    )
    reread = validate_analysis_result(normalized)
    assert reread["memory_score"] == 0.0
    assert reread["details"]["confidence"]["overall"] == 0.0


@pytest.mark.parametrize(
    ("length", "accepted"),
    [(7, False), (8, True), (12, True), (16, True), (17, False)],
)
def test_side_caption_schema_and_validator_share_boundaries(length, accepted):
    property_schema = FULL_ANALYSIS_JSON_SCHEMA["schema"]["properties"]["side_caption"]
    assert property_schema["minLength"] == 8
    assert property_schema["maxLength"] == 16
    candidate = _v3_result(side_caption="字" * length)
    if accepted:
        assert len(validate_analysis_result(candidate)["side_caption"]) == length
    else:
        with pytest.raises(AnalysisValidationError, match="caption／side_caption"):
            validate_analysis_result(candidate)


@pytest.mark.parametrize(("length", "accepted"), [(200, True), (201, False)])
def test_caption_schema_and_validator_share_maximum(length, accepted):
    property_schema = FULL_ANALYSIS_JSON_SCHEMA["schema"]["properties"]["caption"]
    assert property_schema["maxLength"] == 200
    candidate = _v3_result(caption="字" * length)
    if accepted:
        assert len(validate_analysis_result(candidate)["caption"]) == length
    else:
        with pytest.raises(AnalysisValidationError, match="caption／side_caption"):
            validate_analysis_result(candidate)


@pytest.mark.parametrize(
    "updates,match",
    [
        ({"details": []}, "details"),
        ({"grades": []}, "grades"),
        ({"confidence": None}, "confidence"),
        ({"confidence": True}, "confidence"),
        ({"confidence": 2.0}, "confidence"),
        ({"reason_codes": "not-a-list"}, "reason_codes"),
        ({"caption": "short", "side_caption": "too"}, "side_caption"),
        ({"reason": "x" * 101}, "reason"),
        (
            {
                "details": {
                    "memory_grade": "A",
                    "beauty_grade": "B",
                    "technical_grade": "C",
                    "emotion_grade": "D",
                    "confidence": 0.8,
                    "caption_variants": {"unknown": "候選"},
                }
            },
            "caption_variants",
        ),
    ],
)
def test_schema_v3_rejects_malformed_contract_boundaries(updates, match):
    with pytest.raises(AnalysisValidationError, match=match):
        validate_analysis_result(_v3_result(**updates))


def test_set_like_model_arrays_remove_exact_duplicates_before_validation():
    candidate = _v3_result(
        types=["人物", "風景", "人物", "日常"],
        reason_codes=["VISIBLE_PEOPLE", "VISIBLE_PEOPLE"],
        visual_orientation={
            "rotation_cw": 0,
            "confidence": 0.9,
            "ambiguous": False,
            "evidence": ["faces_upright", "faces_upright"],
        },
    )

    normalized = validate_analysis_result(candidate)

    assert normalized["types"] == ["人物", "風景", "日常"]
    assert normalized["reason_codes"] == ["VISIBLE_PEOPLE"]
    assert normalized["visual_orientation"]["evidence"] == ["faces_upright"]


def test_schema_v3_accepts_grade_container_aliases_and_rejects_invalid_grade():
    value = valid_result(
        schema_version=3,
        grades={
            "memory": "A",
            "beauty": "B",
            "technical": "C",
            "emotion": "D",
        },
        details={"confidence": 0.8, "aesthetic_grade": "B"},
    )
    normalized = validate_analysis_result(value)
    assert normalized["beauty_score"] == 70.0
    assert normalized["details"]["beauty_grade"] == "B"

    invalid = _v3_result(
        details={
            "memory_grade": "Z",
            "beauty_grade": "B",
            "technical_grade": "C",
            "emotion_grade": "D",
            "confidence": 0.8,
        }
    )
    with pytest.raises(AnalysisValidationError, match="v3 等級"):
        validate_analysis_result(invalid)


@pytest.mark.parametrize("raw", [[], 1, True, None, "[]", "1"])
def test_analysis_result_top_level_must_be_object(raw):
    with pytest.raises(AnalysisValidationError, match="頂層必須是 JSON Object"):
        validate_analysis_result(raw)


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
