from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any, cast


ALLOWED_TYPES = {
    "人物",
    "孩子",
    "家庭",
    "旅行",
    "風景",
    "美食",
    "寵物",
    "貓咪",
    "日常",
    "活動",
    "建築",
    "夜景",
    "植物",
    "文件",
    "收據",
    "截圖",
    "雜物",
    "其他",
}
BASIC_REQUIRED_FIELDS = {
    "schema_version",
    "caption",
    "types",
    "memory_score",
    "beauty_score",
    "technical_quality_score",
    "emotion_score",
    "side_caption",
    "should_keep",
    "sensitive",
    "reason",
}

# 仍保留舊版基本 Schema，讓既有 Provider 與歷史資料可持續使用；完整 Schema
# 的延伸欄位刻意不是必填，模型不確定時可以省略，而不是補造內容。
REQUIRED_FIELDS = BASIC_REQUIRED_FIELDS
FULL_OPTIONAL_FIELDS = {"details"}
GRADE_VALUES = {"S", "A", "B", "C", "D", "E", "unknown"}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _detail_property(schema: dict) -> dict:
    return _nullable(schema)


class AnalysisValidationError(ValueError):
    code = "VLM-004"


ANALYSIS_JSON_SCHEMA = {
    "name": "inktime_photo_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(REQUIRED_FIELDS),
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "caption": {"type": "string", "minLength": 1, "maxLength": 1000},
            "types": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(ALLOWED_TYPES)},
                "minItems": 1,
                "uniqueItems": True,
            },
            "memory_score": {"type": "number", "minimum": 0, "maximum": 100},
            "beauty_score": {"type": "number", "minimum": 0, "maximum": 100},
            "technical_quality_score": {"type": "number", "minimum": 0, "maximum": 100},
            "emotion_score": {"type": "number", "minimum": 0, "maximum": 100},
            "side_caption": {"type": "string", "maxLength": 120},
            "should_keep": {"type": "boolean"},
            "sensitive": {"type": "boolean"},
            "reason": {"type": "string", "minLength": 1, "maxLength": 240},
        },
    },
}


_DETAIL_PROPERTIES = {
    "memory_grade": _detail_property({"type": "string", "enum": sorted(GRADE_VALUES)}),
    "aesthetic_grade": _detail_property({"type": "string", "enum": sorted(GRADE_VALUES)}),
    "technical_grade": _detail_property({"type": "string", "enum": sorted(GRADE_VALUES)}),
    "emotion_grade": _detail_property({"type": "string", "enum": sorted(GRADE_VALUES)}),
    "display_suitability_grade": _detail_property({"type": "string", "enum": sorted(GRADE_VALUES)}),
    "scene": _detail_property({"type": "string", "maxLength": 80}),
    "setting": _detail_property({"type": "string", "enum": ["indoor", "outdoor", "unknown"]}),
    "time_of_day": _detail_property({"type": "string", "enum": ["day", "night", "unknown"]}),
    "weather": _detail_property({"type": "string", "maxLength": 60}),
    "event_activity": _detail_property({"type": "string", "maxLength": 100}),
    "people_count": _detail_property({"type": "integer", "minimum": 0, "maximum": 100}),
    "people_interaction": _detail_property({"type": "string", "maxLength": 100}),
    "face_visibility": _detail_property({"type": "string", "maxLength": 60}),
    "primary_subject": _detail_property({"type": "string", "maxLength": 120}),
    "objects": _detail_property({"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 12}),
    "animals": _detail_property({"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 8}),
    "food": _detail_property({"type": "boolean"}),
    "vehicles": _detail_property({"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 8}),
    "architecture_type": _detail_property({"type": "string", "maxLength": 80}),
    "landmark_candidates": _detail_property({"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 5}),
    "city_candidate": _detail_property({"type": "string", "maxLength": 80}),
    "country_candidate": _detail_property({"type": "string", "maxLength": 80}),
    "subject_position": _detail_property({"type": "string", "maxLength": 80}),
    "crop_safe": _detail_property({"type": "string", "enum": ["safe", "risk", "unknown"]}),
    "face_crop_risk": _detail_property({"type": "string", "enum": ["low", "medium", "high", "unknown"]}),
    "text_safe_area": _detail_property({"type": "string", "maxLength": 80}),
    "composition_complexity": _detail_property({"type": "string", "enum": ["low", "medium", "high", "unknown"]}),
    "background_clutter": _detail_property({"type": "string", "enum": ["low", "medium", "high", "unknown"]}),
    "epaper_suitability": _detail_property({"type": "string", "maxLength": 100}),
    "skin_detail_risk": _detail_property({"type": "string", "enum": ["low", "medium", "high", "unknown"]}),
    "recommended_preset": _detail_property({"type": "string", "maxLength": 80}),
    "is_screenshot": _detail_property({"type": "boolean"}),
    "is_document": _detail_property({"type": "boolean"}),
    "is_receipt": _detail_property({"type": "boolean"}),
    "short_description": _detail_property({"type": "string", "maxLength": 160}),
    "search_keywords": _detail_property({"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 12}),
    "short_copy": _detail_property({"type": "string", "maxLength": 120}),
    "confidence": _detail_property({"type": "number", "minimum": 0, "maximum": 1}),
}
CAPTION_VARIANT_STYLES = ("natural", "warm", "literary", "humorous", "minimal")

FULL_ANALYSIS_JSON_SCHEMA = {
    "name": "inktime_full_photo_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(BASIC_REQUIRED_FIELDS),
        "properties": {
            **cast(dict[str, Any], ANALYSIS_JSON_SCHEMA["schema"])["properties"],
            "details": {
                "type": "object",
                "additionalProperties": False,
                "properties": _DETAIL_PROPERTIES,
            },
        },
    },
}


def json_schema_for_stage(stage: str, *, caption_controls: dict[str, Any] | None = None) -> dict:
    """完整分析只在高細節單次請求使用；其餘採用成本較低的基本 Schema。"""
    full_stage = stage in {"single_high", "stage_two", "full"}
    if not caption_controls:
        return FULL_ANALYSIS_JSON_SCHEMA if full_stage else ANALYSIS_JSON_SCHEMA
    schema = deepcopy(FULL_ANALYSIS_JSON_SCHEMA if full_stage else ANALYSIS_JSON_SCHEMA)
    properties = cast(dict[str, Any], cast(dict[str, Any], schema["schema"])["properties"])
    properties["caption"].update(
        minLength=int(caption_controls["caption_min_chars"]),
        maxLength=int(caption_controls["caption_max_chars"]),
    )
    properties["side_caption"].update(
        minLength=int(caption_controls["side_caption_min_chars"]),
        maxLength=int(caption_controls["side_caption_max_chars"]),
    )
    if full_stage and bool(caption_controls.get("caption_variants_enabled")):
        properties["details"]["properties"]["caption_variants"] = {
            "type": "object",
            "additionalProperties": False,
            # 個別候選容許省略，避免一個風格缺失使整份分析無法使用。
            "properties": {
                style: {
                    "type": "string",
                    "minLength": int(caption_controls["side_caption_min_chars"]),
                    "maxLength": int(caption_controls["side_caption_max_chars"]),
                }
                for style in CAPTION_VARIANT_STYLES
            },
        }
    return schema


def _score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisValidationError(f"{field} 必須是數字")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise AnalysisValidationError(f"{field} 必須介於 0 到 100")
    return result


def validate_analysis_result(raw: str | dict) -> dict:
    if isinstance(raw, str):
        if "```" in raw:
            raise AnalysisValidationError("不可使用 Markdown code fence")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            error = AnalysisValidationError("模型回傳無效 JSON")
            error.code = "VLM-003"
            raise error from exc
    else:
        value = dict(raw)
    allowed = BASIC_REQUIRED_FIELDS | FULL_OPTIONAL_FIELDS
    if not BASIC_REQUIRED_FIELDS <= set(value) or not set(value) <= allowed:
        missing = sorted(BASIC_REQUIRED_FIELDS - set(value))
        extra = sorted(set(value) - allowed)
        raise AnalysisValidationError(f"欄位不符合 Schema；缺少={missing}，多餘={extra}")
    if value["schema_version"] not in {1, 2}:
        raise AnalysisValidationError("不支援的 schema_version")
    if not isinstance(value["caption"], str) or not value["caption"].strip():
        raise AnalysisValidationError("caption 不可空白")
    if not isinstance(value["side_caption"], str) or len(value["side_caption"]) > 120:
        raise AnalysisValidationError("side_caption 格式不合法")
    if not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 240:
        raise AnalysisValidationError("reason 格式不合法")
    types = value["types"]
    if (
        not isinstance(types, list)
        or not types
        or len(types) != len(set(types))
        or any(item not in ALLOWED_TYPES for item in types)
    ):
        raise AnalysisValidationError("types 含有不允許或重複的類型")
    if not isinstance(value["should_keep"], bool) or not isinstance(value["sensitive"], bool):
        raise AnalysisValidationError("布林欄位格式不合法")
    for field in ("memory_score", "beauty_score", "technical_quality_score", "emotion_score"):
        value[field] = _score(value[field], field)
    value["caption"] = value["caption"].strip()
    value["side_caption"] = value["side_caption"].strip()
    value["reason"] = value["reason"].strip()
    details = value.get("details")
    if details is not None:
        if not isinstance(details, dict) or not set(details) <= (set(_DETAIL_PROPERTIES) | {"caption_variants"}):
            raise AnalysisValidationError("details 欄位不合法")
        for field, detail in details.items():
            if field == "caption_variants":
                if not isinstance(detail, dict) or not set(detail) <= set(CAPTION_VARIANT_STYLES):
                    raise AnalysisValidationError("caption_variants 欄位不合法")
                if any(not isinstance(value, str) or not value.strip() for value in detail.values()):
                    raise AnalysisValidationError("caption_variants 必須是非空白文字")
                details[field] = {style: value.strip() for style, value in detail.items()}
                continue
            if isinstance(detail, str):
                details[field] = detail.strip()
    return value
