from __future__ import annotations

import pytest

from inktime.app.domain.analysis.json_repair import (
    assert_semantic_repair_integrity,
    extract_json_value,
    repair_source_object,
    semantic_repair_snapshot,
)
from inktime.app.domain.analysis.schema import AnalysisValidationError
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


def test_repair_source_accepts_only_unambiguous_safe_syntax_conversion():
    expected = valid_result()
    assert repair_source_object(repr(expected)) == expected
    assert repair_source_object("not-json") is None
    assert repair_source_object("[{'memory_score': 72}]") is None


def test_semantic_snapshot_requires_every_protected_field_and_is_immutable():
    source = valid_result()
    snapshot = semantic_repair_snapshot(source)
    source["memory_score"] = 1
    assert snapshot["memory_score"] == 72

    missing = valid_result()
    del missing["people_count"]
    with pytest.raises(AnalysisValidationError, match="重新執行 Vision Analysis") as exc:
        semantic_repair_snapshot(missing)
    assert exc.value.code == "VLM-007"


@pytest.mark.parametrize(
    "field",
    [
        "memory_score", "visual_score", "special_level", "special_codes",
        "people_count", "content_filter", "visual_orientation",
        "subject_position", "text_safe_area",
    ],
)
def test_repair_cannot_change_or_invent_semantic_values(field):
    source = valid_result()
    snapshot = semantic_repair_snapshot(source)
    repaired = valid_result()
    repaired[field] = "changed"
    with pytest.raises(AnalysisValidationError, match="重新執行 Vision Analysis"):
        assert_semantic_repair_integrity(snapshot, repaired)
