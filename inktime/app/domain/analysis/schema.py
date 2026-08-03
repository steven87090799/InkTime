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
    "visual_orientation",
}
ORIENTATION_EVIDENCE = {
    "faces_upright",
    "text_upright",
    "horizon_level",
    "gravity_objects",
    "architecture_vertical",
    "insufficient_visual_cues",
}

# 仍保留舊版基本 Schema，讓既有 Provider 與歷史資料可持續使用；完整 Schema
# 的延伸欄位刻意不是必填，模型不確定時可以省略，而不是補造內容。
REQUIRED_FIELDS = BASIC_REQUIRED_FIELDS
FULL_OPTIONAL_FIELDS = {"details"}
GRADE_VALUES = {"S", "A", "B", "C", "D", "E", "unknown"}
V3_GRADE_FIELDS = {
    "memory_grade": "memory_score",
    "beauty_grade": "beauty_score",
    "technical_grade": "technical_quality_score",
    "emotion_grade": "emotion_score",
}
V3_GRADE_ALIASES = {
    "aesthetic_grade": "beauty_grade",
    "aesthetic": "beauty_grade",
    "memory": "memory_grade",
    "beauty": "beauty_grade",
    "technical": "technical_grade",
    "emotion": "emotion_grade",
}
V3_TOP_LEVEL_OPTIONAL_FIELDS = {"reason_codes"}


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
            "schema_version": {"type": "integer", "const": 2},
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
            "visual_orientation": {
                "type": "object",
                "additionalProperties": False,
                "required": ["rotation_cw", "confidence", "ambiguous", "evidence"],
                "properties": {
                    "rotation_cw": {
                        "anyOf": [{"type": "integer", "enum": [0, 90, 180, 270]}, {"type": "null"}]
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "ambiguous": {"type": "boolean"},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(ORIENTATION_EVIDENCE)},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                },
            },
        },
    },
}


_DETAIL_PROPERTIES = {
    "memory_grade": _detail_property({"type": "string", "enum": sorted(GRADE_VALUES)}),
    "beauty_grade": _detail_property({"type": "string", "enum": sorted(GRADE_VALUES)}),
    # Historical semantic_json rows may still carry this spelling.
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
    "subjects": _detail_property(
        {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 5}
    ),
    "objects": _detail_property(
        {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 12}
    ),
    "animals": _detail_property(
        {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 8}
    ),
    "food": _detail_property({"type": "boolean"}),
    "vehicles": _detail_property(
        {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 8}
    ),
    "architecture_type": _detail_property({"type": "string", "maxLength": 80}),
    "landmark_candidates": _detail_property(
        {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 5}
    ),
    "city_candidate": _detail_property({"type": "string", "maxLength": 80}),
    "country_candidate": _detail_property({"type": "string", "maxLength": 80}),
    "subject_position": _detail_property({"type": "string", "maxLength": 80}),
    "crop_safe": _detail_property({"type": "string", "enum": ["safe", "risk", "unknown"]}),
    "face_crop_risk": _detail_property({"type": "string", "enum": ["low", "medium", "high", "unknown"]}),
    "text_safe_area": _detail_property({"type": "string", "maxLength": 80}),
    "composition_complexity": _detail_property(
        {"type": "string", "enum": ["low", "medium", "high", "unknown"]}
    ),
    "background_clutter": _detail_property({"type": "string", "enum": ["low", "medium", "high", "unknown"]}),
    "epaper_suitability": _detail_property({"type": "string", "maxLength": 100}),
    "skin_detail_risk": _detail_property({"type": "string", "enum": ["low", "medium", "high", "unknown"]}),
    "recommended_preset": _detail_property({"type": "string", "maxLength": 80}),
    "is_screenshot": _detail_property({"type": "boolean"}),
    "is_document": _detail_property({"type": "boolean"}),
    "is_receipt": _detail_property({"type": "boolean"}),
    "short_description": _detail_property({"type": "string", "maxLength": 160}),
    "search_keywords": _detail_property(
        {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 12}
    ),
    "short_copy": _detail_property({"type": "string", "maxLength": 120}),
    "confidence": _detail_property(
        {
            "anyOf": [
                {"type": "number", "minimum": 0, "maximum": 1},
                {
                    "type": "object",
                    "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
                },
            ]
        }
    ),
}
CAPTION_VARIANT_STYLES = ("natural", "warm", "literary", "humorous", "minimal")

FULL_ANALYSIS_JSON_SCHEMA = {
    "name": "inktime_full_photo_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(
            (BASIC_REQUIRED_FIELDS - set(V3_GRADE_FIELDS.values()))
            | set(V3_GRADE_FIELDS)
            | {"confidence"}
        ),
        "properties": {
            **cast(dict[str, Any], ANALYSIS_JSON_SCHEMA["schema"])["properties"],
            "schema_version": {"type": "integer", "const": 3},
            "caption": {"type": "string", "minLength": 1, "maxLength": 200},
            "side_caption": {"type": "string", "minLength": 8, "maxLength": 16},
            "types": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(ALLOWED_TYPES)},
                "minItems": 1,
                "maxItems": 5,
                "uniqueItems": True,
            },
            "reason": {"type": "string", "minLength": 1, "maxLength": 100},
            **{
                field: {"type": "string", "enum": sorted(GRADE_VALUES)}
                for field in V3_GRADE_FIELDS
            },
            "confidence": {
                "anyOf": [
                    {"type": "number", "minimum": 0, "maximum": 1},
                    {
                        "type": "object",
                        "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                ]
            },
            "reason_codes": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 60},
                "maxItems": 5,
                "uniqueItems": True,
            },
            "details": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    **_DETAIL_PROPERTIES,
                    "scene": _detail_property({"type": "string", "maxLength": 60}),
                    "landmark_candidates": _detail_property(
                        {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 3}
                    ),
                    "search_keywords": _detail_property(
                        {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 8}
                    ),
                },
            },
        },
    },
}


def json_schema_for_stage(stage: str, *, caption_controls: dict[str, Any] | None = None) -> dict:
    """完整分析只在高細節單次請求使用；其餘採用成本較低的基本 Schema。"""
    full_stage = stage in {"single", "single_high", "stage_two", "full"}
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


def _grade_score(value: Any) -> float:
    values = {"S": 95.0, "A": 85.0, "B": 75.0, "C": 60.0, "D": 40.0, "E": 20.0, "unknown": 0.0}
    if value not in values:
        raise AnalysisValidationError("v3 等級必須是 S/A/B/C/D/E/unknown")
    return values[value]


def _normalize_v3(value: dict) -> dict:
    """Convert the grade-oriented response to the stable persistence shape."""
    normalized = dict(value)
    raw_details = normalized.get("details")
    if raw_details is not None and not isinstance(raw_details, dict):
        raise AnalysisValidationError("details 必須是 JSON Object")
    details = dict(raw_details or {})
    grades = normalized.pop("grades", None)
    if grades is not None:
        if not isinstance(grades, dict):
            raise AnalysisValidationError("grades 必須是 JSON Object")
        for key, grade in grades.items():
            canonical = V3_GRADE_ALIASES.get(str(key), str(key))
            if canonical in V3_GRADE_FIELDS:
                details[canonical] = grade
    for key in list(details):
        canonical = V3_GRADE_ALIASES.get(str(key), str(key))
        if canonical in V3_GRADE_FIELDS:
            details[canonical] = details.pop(key)
    for field in (*V3_GRADE_FIELDS, *V3_GRADE_ALIASES):
        if field in normalized:
            details[V3_GRADE_ALIASES.get(field, field)] = normalized.pop(field)
    if "display_suitability_grade" in normalized:
        details["display_suitability_grade"] = normalized.pop("display_suitability_grade")
    confidence_present = "confidence" in normalized
    confidence = normalized.pop("confidence", None)
    if confidence_present:
        if confidence is None:
            raise AnalysisValidationError("confidence 不可為 null")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float, dict)):
            raise AnalysisValidationError("confidence 必須是 0 到 1 的數字或 Object")
        details["confidence"] = confidence
    for grade_field, score_field in V3_GRADE_FIELDS.items():
        if grade_field not in details:
            raise AnalysisValidationError(f"schema v3 缺少 details.{grade_field}")
        normalized[score_field] = _grade_score(details[grade_field])
    if "confidence" not in details:
        raise AnalysisValidationError("schema v3 缺少 details.confidence")
    normalized["details"] = details
    return normalized


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
    elif isinstance(raw, dict):
        value = dict(raw)
    else:
        error = AnalysisValidationError("模型回傳頂層必須是 JSON Object")
        error.code = "VLM-003"
        raise error
    if not isinstance(value, dict):
        error = AnalysisValidationError("模型回傳頂層必須是 JSON Object")
        error.code = "VLM-003"
        raise error
    if value.get("schema_version") == 3:
        details = value.get("details")
        if details is not None and not isinstance(details, dict):
            raise AnalysisValidationError("details 必須是 JSON Object")
        grades = value.get("grades")
        if grades is not None and not isinstance(grades, dict):
            raise AnalysisValidationError("grades 必須是 JSON Object")
        sources = dict(details or {})
        sources.update(dict(grades or {}))
        sources.update(value)
        for canonical in V3_GRADE_FIELDS:
            aliases = [alias for alias, target in V3_GRADE_ALIASES.items() if target == canonical]
            if not any(
                key in sources
                for key in (canonical, *aliases)
            ):
                raise AnalysisValidationError(f"schema v3 缺少 {canonical}")
        if "confidence" not in sources:
            raise AnalysisValidationError("schema v3 缺少 confidence")
    if value.get("schema_version") == 3:
        value = _normalize_v3(value)
    # v1 cache entries predate this additive field.  Keep them readable without
    # treating the missing value as a confident orientation recommendation.
    if value.get("schema_version") == 1 and "visual_orientation" not in value:
        value["visual_orientation"] = {
            "rotation_cw": None,
            "confidence": 0.0,
            "ambiguous": True,
            "evidence": ["insufficient_visual_cues"],
        }
    allowed = BASIC_REQUIRED_FIELDS | FULL_OPTIONAL_FIELDS
    if value.get("schema_version") == 3:
        allowed |= V3_TOP_LEVEL_OPTIONAL_FIELDS
    if not BASIC_REQUIRED_FIELDS <= set(value) or not set(value) <= allowed:
        missing = sorted(BASIC_REQUIRED_FIELDS - set(value))
        extra = sorted(set(value) - allowed)
        raise AnalysisValidationError(f"欄位不符合 Schema；缺少={missing}，多餘={extra}")
    if value["schema_version"] not in {1, 2, 3}:
        raise AnalysisValidationError("不支援的 schema_version")
    if value["schema_version"] == 2 and "visual_orientation" not in value:
        raise AnalysisValidationError("schema v2 必須包含 visual_orientation")
    if value["schema_version"] == 3:
        details = value.get("details") or {}
        for grade_field in (*V3_GRADE_FIELDS, "display_suitability_grade"):
            if grade_field in details and details[grade_field] not in GRADE_VALUES:
                raise AnalysisValidationError(f"{grade_field} 等級不合法")
        confidence = details.get("confidence")
        if confidence is None:
            raise AnalysisValidationError("schema v3 confidence 不可為 null")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float, dict)):
            raise AnalysisValidationError("confidence 格式不合法")
        if isinstance(confidence, dict) and any(
            isinstance(item, bool) or not isinstance(item, (int, float)) or not 0 <= float(item) <= 1
            for item in confidence.values()
        ):
            raise AnalysisValidationError("confidence Object 值必須介於 0 到 1")
        if isinstance(confidence, (int, float)) and not 0 <= float(confidence) <= 1:
            raise AnalysisValidationError("confidence 必須介於 0 到 1")
        reason_codes = value.get("reason_codes", [])
        if not isinstance(reason_codes, list) or len(reason_codes) > 5:
            raise AnalysisValidationError("reason_codes 格式不合法")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 60 for item in reason_codes):
            raise AnalysisValidationError("reason_codes 格式不合法")
        if len(reason_codes) != len(set(reason_codes)):
            raise AnalysisValidationError("reason_codes 格式不合法")
    if not isinstance(value["caption"], str) or not value["caption"].strip():
        raise AnalysisValidationError("caption 不可空白")
    if not isinstance(value["side_caption"], str) or len(value["side_caption"]) > 120:
        raise AnalysisValidationError("side_caption 格式不合法")
    if not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 240:
        raise AnalysisValidationError("reason 格式不合法")
    if value.get("schema_version") == 3:
        if len(value["caption"]) > 200 or not 8 <= len(value["side_caption"]) <= 16:
            raise AnalysisValidationError("v3 caption／side_caption 長度不合法")
        if len(value["reason"]) > 100 or len(value["types"]) > 5:
            raise AnalysisValidationError("v3 reason／types 長度不合法")
    types = value["types"]
    if not isinstance(types, list) or not types:
        raise AnalysisValidationError("types 含有不允許或重複的類型")
    if any(not isinstance(item, str) or item not in ALLOWED_TYPES for item in types):
        raise AnalysisValidationError("types 含有不允許或重複的類型")
    if len(types) != len(set(types)):
        raise AnalysisValidationError("types 含有不允許或重複的類型")
    if not isinstance(value["should_keep"], bool) or not isinstance(value["sensitive"], bool):
        raise AnalysisValidationError("布林欄位格式不合法")
    for field in ("memory_score", "beauty_score", "technical_quality_score", "emotion_score"):
        value[field] = _score(value[field], field)
    orientation = value["visual_orientation"]
    if not isinstance(orientation, dict) or set(orientation) != {
        "rotation_cw",
        "confidence",
        "ambiguous",
        "evidence",
    }:
        raise AnalysisValidationError("visual_orientation 欄位不合法")
    rotation = orientation["rotation_cw"]
    if rotation is not None and (isinstance(rotation, bool) or not isinstance(rotation, int) or rotation not in {0, 90, 180, 270}):
        raise AnalysisValidationError("visual_orientation.rotation_cw 不合法")
    if (
        isinstance(orientation["confidence"], bool)
        or not isinstance(orientation["confidence"], (int, float))
        or not 0 <= float(orientation["confidence"]) <= 1
    ):
        raise AnalysisValidationError("visual_orientation.confidence 不合法")
    if (
        not isinstance(orientation["ambiguous"], bool)
        or not isinstance(orientation["evidence"], list)
        or not orientation["evidence"]
        or any(not isinstance(item, str) or item not in ORIENTATION_EVIDENCE for item in orientation["evidence"])
    ):
        raise AnalysisValidationError("visual_orientation evidence 不合法")
    if len(orientation["evidence"]) != len(set(orientation["evidence"])):
        raise AnalysisValidationError("visual_orientation evidence 不合法")
    if "insufficient_visual_cues" in orientation["evidence"] and orientation["evidence"] != [
        "insufficient_visual_cues"
    ]:
        raise AnalysisValidationError("insufficient_visual_cues 不可與其他證據混用")
    if orientation["rotation_cw"] is None and (
        not orientation["ambiguous"] or orientation["evidence"] != ["insufficient_visual_cues"]
    ):
        raise AnalysisValidationError("方向不明必須標示 insufficient_visual_cues")
    orientation["confidence"] = float(orientation["confidence"])
    value["caption"] = value["caption"].strip()
    value["side_caption"] = value["side_caption"].strip()
    value["reason"] = value["reason"].strip()
    details = value.get("details")
    if details is not None:
        if not isinstance(details, dict) or not set(details) <= (
            set(_DETAIL_PROPERTIES) | {"caption_variants"}
        ):
            raise AnalysisValidationError("details 欄位不合法")
        for field, detail in details.items():
            if field == "caption_variants":
                if not isinstance(detail, dict) or not set(detail) <= set(CAPTION_VARIANT_STYLES):
                    raise AnalysisValidationError("caption_variants 欄位不合法")
                if any(not isinstance(value, str) or not value.strip() for value in detail.values()):
                    raise AnalysisValidationError("caption_variants 必須是非空白文字")
                details[field] = {style: value.strip() for style, value in detail.items()}
                continue
            if field in {"subjects", "objects", "animals", "vehicles", "landmark_candidates", "search_keywords"}:
                if not isinstance(detail, list) or any(
                    not isinstance(item, str) or not item.strip() for item in detail
                ):
                    raise AnalysisValidationError(f"{field} 必須是非空白文字陣列")
                limits = {"subjects": 5, "landmark_candidates": 3, "search_keywords": 8}
                if value.get("schema_version") == 3 and len(detail) > limits.get(field, 12):
                    raise AnalysisValidationError(f"{field} 超過數量上限")
                details[field] = [item.strip() for item in detail]
                continue
            if isinstance(detail, str):
                details[field] = detail.strip()
    return value
