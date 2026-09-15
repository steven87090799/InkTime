from __future__ import annotations

import json

import pytest

from inktime.app.domain.analysis.json_repair import extract_json_value
from inktime.app.domain.analysis.schema import AnalysisValidationError, validate_model_response
from tests.unit.test_analysis_schema import valid_result


def test_json_repair_accepts_one_object_and_rejects_ambiguous_containers():
    expected = {"schema_version": 2, "caption": "單一物件"}
    assert extract_json_value('{"schema_version":2,"caption":"單一物件"}') == expected
    assert extract_json_value("```json\n{" + '"schema_version":2,"caption":"單一物件"' + "}\n```") == expected
    assert extract_json_value("模型前綴：{" + '"schema_version":2,"caption":"單一物件"' + "}") == expected
    assert extract_json_value("[{\"schema_version\":2}]") is None
    assert extract_json_value('{"schema_version":2}{"schema_version":2}') is None
    assert extract_json_value("第一個 {\"schema_version\":2} 第二個 {\"schema_version\":2}") is None
    assert extract_json_value("") is None
    assert extract_json_value("```json\nnot-json\n```") is None
    assert extract_json_value("```json\r\n{}\r\n```") == {}
    assert extract_json_value("前綴 {not-json} 後綴") is None


@pytest.mark.parametrize(
    "wrapped",
    [
        lambda raw: f"```json\n{raw}\n```",
        lambda raw: f"模型前綴：{raw} 分析完成",
    ],
)
def test_local_extraction_recovers_wrapped_v5_json(wrapped):
    expected = valid_result()
    extracted = extract_json_value(wrapped(json.dumps(expected, ensure_ascii=False)))
    assert validate_model_response(extracted) == expected


def test_missing_or_invalid_semantic_value_is_vlm_004():
    missing = valid_result()
    del missing["side_caption"]
    for value in (missing, valid_result(memory_score="72"), "not-json"):
        with pytest.raises(AnalysisValidationError) as raised:
            validate_model_response(value)
        assert raised.value.code == "VLM-004"
