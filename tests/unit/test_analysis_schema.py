from __future__ import annotations

import json
import pytest

from inktime.app.domain.analysis.schema import (
    ANALYSIS_JSON_SCHEMA,
    AnalysisValidationError,
    REQUIRED_FIELDS,
    json_schema_for_stage,
    validate_analysis_result,
)


def content_filter_result(code=None, confidence=0.97, **updates):
    value = {
        category: {"detected": category == code, "confidence": confidence}
        for category in ("sexualized_content", "explicit_nudity", "female_glamour_portrait")
    }
    value.update(updates)
    return value


def valid_result(**updates):
    value = {
        "schema_version": 4,
        "types": ["人物", "日常"],
        "memory_score": 72,
        "visual_score": 81,
        "special_level": 2,
        "special_codes": ["meaningful_activity"],
        "people_count": 1,
        "caption": "男子在河畔持釣竿，背景為開闊天空與河岸，身旁草地上的釣具清楚可見。",
        "side_caption": "釣竿伸向雲層深處。",
        "content_filter": content_filter_result(),
        "subject_position": "center",
        "text_safe_area": "bottom_right",
        "visual_orientation": {
            "rotation_cw": 0,
            "confidence": 0.97,
            "ambiguous": False,
            "evidence": ["faces_upright", "horizon_level"],
        },
    }
    value.update(updates)
    return value


def test_strict_schema_accepts_expected_result():
    assert validate_analysis_result(json.dumps(valid_result(), ensure_ascii=False)) == valid_result()


@pytest.mark.parametrize("field", sorted(REQUIRED_FIELDS))
def test_every_v4_field_is_required(field):
    value = valid_result()
    del value[field]
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(value)


@pytest.mark.parametrize(
    "extra",
    [
        "memory_grade",
        "beauty_grade",
        "technical_quality_score",
        "emotion_score",
        "reason",
        "reason_codes",
        "details",
        "should_keep",
        "bonus",
    ],
)
def test_rejects_obsolete_or_server_owned_fields(extra):
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(valid_result(**{extra: 1}))


@pytest.mark.parametrize("version", [1, 2, 3, 5, True, 4.0])
def test_only_v4_is_supported(version):
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(valid_result(schema_version=version))


@pytest.mark.parametrize(
    "field,value",
    [
        ("memory_score", True),
        ("memory_score", -1),
        ("visual_score", 101),
        ("visual_score", float("nan")),
        ("visual_score", float("inf")),
        ("memory_score", "72"),
        ("special_level", 2.5),
        ("special_level", True),
        ("special_level", 5),
        ("people_count", -1),
        ("people_count", False),
        ("types", []),
        ("types", ["人物", "人物"]),
        ("types", ["人物", "日常", "活動", "旅行"]),
        ("special_codes", ["group_photo", "milestone", "ceremony"]),
        ("special_codes", ["rare_in_your_library"]),
        ("caption", "短"),
        ("caption", "字" * 101),
        ("side_caption", "字" * 7),
        ("side_caption", "字" * 17),
        ("text_safe_area", None),
        ("content_filter", {"exclude_code": "screenshot", "confidence": 0.99}),
        ("content_filter", {"exclude_code": "none", "confidence": 0.99, "exclude": True}),
        ("content_filter", {"exclude_code": "none", "confidence": 0.99}),
    ],
)
def test_invalid_values_fail(field, value):
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(valid_result(**{field: value}))


@pytest.mark.parametrize(
    "rotation,confidence,ambiguous,evidence,valid",
    [
        (None, 0.95, True, ["insufficient_visual_cues"], False),
        (None, 0.2, True, ["insufficient_visual_cues"], True),
        (None, 0.2, False, ["insufficient_visual_cues"], False),
        (90, 0.95, False, ["faces_upright"], True),
        (270, 0.9, False, ["text_upright"], True),
        (None, 0.5, True, ["insufficient_visual_cues"], True),
        (0, 0.2, False, ["insufficient_visual_cues"], False),
        (180, 0.9, False, ["insufficient_visual_cues"], False),
        (0, 0.7, False, ["insufficient_visual_cues", "faces_upright"], False),
    ],
)
def test_orientation_consistency(rotation, confidence, ambiguous, evidence, valid):
    value = valid_result(
        visual_orientation=dict(
            rotation_cw=rotation, confidence=confidence, ambiguous=ambiguous, evidence=evidence
        )
    )
    if valid:
        assert validate_analysis_result(value)["visual_orientation"] == value["visual_orientation"]
    else:
        with pytest.raises(AnalysisValidationError):
            validate_analysis_result(value)


@pytest.mark.parametrize("stage", ["single", "full", "stage_one", "scoring_test"])
def test_single_schema_for_every_photo_stage(stage):
    assert json_schema_for_stage(stage) == ANALYSIS_JSON_SCHEMA
    schema = json_schema_for_stage(stage, caption_controls={"caption_variants_enabled": True})["schema"]
    assert set(schema["properties"]) == REQUIRED_FIELDS
    assert not schema["additionalProperties"]
    for key in ("content_filter", "visual_orientation"):
        assert not schema["properties"][key]["additionalProperties"]


def test_text_is_traditional_chinese():
    result = validate_analysis_result(
        valid_result(
            caption="他们在复古小镇看着远处风景，街道旁的树影与石板小路清楚可见。",
            side_caption="树影沿着小路慢慢展开。",
        )
    )
    assert "他們" in result["caption"] and "樹影" in result["side_caption"]


def test_caption_controls_cannot_publish_a_contract_the_validator_rejects():
    schema = json_schema_for_stage('full', caption_controls={
        'caption_min_chars': 0, 'caption_target_chars': 10, 'caption_max_chars': 20,
    })['schema']['properties']['caption']
    assert schema['minLength'] == 10 and schema['maxLength'] == 20


@pytest.mark.parametrize("code", ["sexualized_content", "explicit_nudity", "female_glamour_portrait"])
@pytest.mark.parametrize("invalid", [
    {"detected": True},
    {"detected": "true", "confidence": 0.95},
    {"detected": 1, "confidence": 0.95},
    {"detected": True, "confidence": float("nan")},
    {"detected": False, "confidence": 1.1},
    {"detected": True, "confidence": 0.95, "exclude": True},
])
def test_each_content_classification_is_strict(code, invalid):
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(valid_result(content_filter=content_filter_result(**{code: invalid})))


@pytest.mark.parametrize("code", ["sexualized_content", "explicit_nudity", "female_glamour_portrait"])
def test_no_content_classification_can_be_omitted(code):
    content = content_filter_result()
    del content[code]
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(valid_result(content_filter=content))
