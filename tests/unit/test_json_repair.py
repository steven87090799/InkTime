from __future__ import annotations

from inktime.app.domain.analysis.json_repair import extract_json_value


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
