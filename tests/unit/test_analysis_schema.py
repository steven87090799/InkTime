from __future__ import annotations

import json

import pytest

from inktime.app.domain.analysis.schema import AnalysisValidationError, validate_analysis_result


def valid_result(**updates):
    value = {
        "schema_version": 1,
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
    }
    value.update(updates)
    return value


def test_strict_schema_accepts_expected_result():
    result = validate_analysis_result(json.dumps(valid_result(), ensure_ascii=False))
    assert result["memory_score"] == 82


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
