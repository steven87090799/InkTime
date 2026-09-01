from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

from inktime.app.domain.analysis.traditional_chinese import to_taiwan_traditional

SCHEMA_VERSION = 4
CAPTION_MAX_CHARS = 100
SIDE_CAPTION_MIN_CHARS = 8
SIDE_CAPTION_MAX_CHARS = 16
CAPTION_VARIANT_STYLES = ("natural", "warm", "literary", "humorous", "minimal")
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
SPECIAL_CODES = {
    "group_photo",
    "meaningful_activity",
    "major_event",
    "rare_moment",
    "milestone",
    "travel_memory",
    "family_moment",
    "strong_interaction",
    "ceremony",
    "performance",
}
CONTENT_FILTER_CODES = {
    "none",
    "sexualized_content",
    "explicit_nudity",
    "female_glamour_portrait",
    "uncertain",
}
ORIENTATION_EVIDENCE = {
    "faces_upright",
    "text_upright",
    "horizon_level",
    "gravity_objects",
    "architecture_vertical",
    "insufficient_visual_cues",
}
POSITIONS = [
    "center",
    "left",
    "right",
    "top",
    "bottom",
    "top_left",
    "top_right",
    "bottom_left",
    "bottom_right",
    "unknown",
]


def _object(properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(properties),
        "properties": properties,
    }


def _enum_array(values, minimum=0, maximum=5) -> dict:
    return {
        "type": "array",
        "items": {"type": "string", "enum": sorted(values)},
        "minItems": minimum,
        "maxItems": maximum,
        "uniqueItems": True,
    }


ANALYSIS_JSON_SCHEMA: dict[str, Any] = {
    "name": "inktime_photo_analysis",
    "strict": True,
    "schema": _object(
        {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "types": _enum_array(ALLOWED_TYPES, 1, 3),
            "memory_score": {"type": "number", "minimum": 0, "maximum": 100},
            "visual_score": {"type": "number", "minimum": 0, "maximum": 100},
            "special_level": {"type": "integer", "minimum": 0, "maximum": 4},
            "special_codes": _enum_array(SPECIAL_CODES, 0, 2),
            "people_count": {"type": "integer", "minimum": 0, "maximum": 10000},
            "caption": {"type": "string", "minLength": 10, "maxLength": CAPTION_MAX_CHARS},
            "side_caption": {
                "type": "string",
                "minLength": SIDE_CAPTION_MIN_CHARS,
                "maxLength": SIDE_CAPTION_MAX_CHARS,
            },
            "content_filter": _object(
                {
                    "exclude_code": {"type": "string", "enum": sorted(CONTENT_FILTER_CODES)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                }
            ),
            "subject_position": {"type": "string", "enum": POSITIONS},
            "text_safe_area": {"type": "string", "enum": [*POSITIONS, "none"]},
            "visual_orientation": _object(
                {
                    "rotation_cw": {
                        "anyOf": [{"type": "integer", "enum": [0, 90, 180, 270]}, {"type": "null"}]
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "ambiguous": {"type": "boolean"},
                    "evidence": _enum_array(ORIENTATION_EVIDENCE, 1, 6),
                }
            ),
        }
    ),
}
FULL_ANALYSIS_JSON_SCHEMA = ANALYSIS_JSON_SCHEMA
REQUIRED_FIELDS = set(ANALYSIS_JSON_SCHEMA["schema"]["required"])


PROVIDER_CONTRACT_JSON_SCHEMA: dict[str, Any] = {
    "name": "inktime_provider_vision_contract",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["vision_ok", "detected_shapes"],
        "properties": {
            "vision_ok": {"type": "boolean"},
            "detected_shapes": {
                "type": "array",
                "items": {"type": "string", "enum": ["rectangle", "circle"]},
                "minItems": 2,
                "maxItems": 2,
                "uniqueItems": True,
            },
        },
    },
}


def normalize_caption_controls(controls: dict[str, Any]) -> dict[str, Any]:
    """Bound persisted caption settings to the v4 schema/validator contract."""
    normalized = dict(controls)
    caption_upper = max(10, min(CAPTION_MAX_CHARS, int(controls.get("caption_max_chars", CAPTION_MAX_CHARS))))
    caption_target = max(10, min(caption_upper, int(controls.get("caption_target_chars", 60))))
    caption_minimum = max(10, min(caption_target, int(controls.get("caption_min_chars", 10))))
    side_upper = max(
        SIDE_CAPTION_MIN_CHARS,
        min(SIDE_CAPTION_MAX_CHARS, int(controls.get("side_caption_max_chars", SIDE_CAPTION_MAX_CHARS))),
    )
    side_target = max(
        SIDE_CAPTION_MIN_CHARS, min(side_upper, int(controls.get("side_caption_target_chars", 12)))
    )
    side_minimum = max(
        SIDE_CAPTION_MIN_CHARS,
        min(side_target, int(controls.get("side_caption_min_chars", SIDE_CAPTION_MIN_CHARS))),
    )
    normalized.update(
        caption_min_chars=caption_minimum,
        caption_target_chars=caption_target,
        caption_max_chars=caption_upper,
        side_caption_min_chars=side_minimum,
        side_caption_target_chars=side_target,
        side_caption_max_chars=side_upper,
    )
    return normalized


class AnalysisValidationError(ValueError):
    code = "VLM-004"


def json_schema_for_stage(
    stage: str, *, caption_controls: dict[str, Any] | None = None
) -> dict[str, Any]:
    if stage == "provider_contract_level2":
        return deepcopy(PROVIDER_CONTRACT_JSON_SCHEMA)
    schema = deepcopy(ANALYSIS_JSON_SCHEMA)
    if caption_controls:
        controls = normalize_caption_controls(caption_controls)
        for field in ("caption", "side_caption"):
            schema["schema"]["properties"][field].update(
                minLength=controls[f"{field}_min_chars"], maxLength=controls[f"{field}_max_chars"]
            )
    return schema


def _validate(value: Any, schema: dict[str, Any], path: str) -> None:
    if "anyOf" in schema:
        for choice in schema["anyOf"]:
            try:
                _validate(value, choice, path)
                return
            except AnalysisValidationError:
                pass
        raise AnalysisValidationError(f"{path} 格式不合法")
    kind = schema["type"]
    valid_type = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "null": value is None,
    }[kind]
    if not valid_type:
        raise AnalysisValidationError(f"{path} 必須是 {kind}")
    if "const" in schema and value != schema["const"]:
        raise AnalysisValidationError(f"{path} 不支援的版本")
    if "enum" in schema and value not in schema["enum"]:
        raise AnalysisValidationError(f"{path} 不允許的值")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise AnalysisValidationError(f"{path} 欄位不符合 Schema")
        for key, item in value.items():
            _validate(item, schema["properties"][key], f"{path}.{key}")
    elif kind == "array":
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise AnalysisValidationError(f"{path} 數量不合法")
        for item in value:
            _validate(item, schema["items"], path)
        if len(set(value)) != len(value):
            raise AnalysisValidationError(f"{path} 不可重複")
    elif kind in {"integer", "number"}:
        if not math.isfinite(value) or not schema.get("minimum", value) <= value <= schema.get(
            "maximum", value
        ):
            raise AnalysisValidationError(f"{path} 數值超出範圍")
    elif kind == "string" and not schema.get("minLength", 0) <= len(value.strip()) <= schema.get(
        "maxLength", len(value)
    ):
        raise AnalysisValidationError(f"{path} 長度不合法")


def validate_analysis_result(raw: str | dict) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            error = AnalysisValidationError("模型回傳無效 JSON")
            error.code = "VLM-003"
            raise error from exc
    value = to_taiwan_traditional(deepcopy(raw))
    _validate(value, ANALYSIS_JSON_SCHEMA["schema"], "analysis")
    if not isinstance(value, dict):  # Narrow the type after the schema validator accepts it.
        raise AnalysisValidationError("analysis 必須是 object")
    orientation = value["visual_orientation"]
    if orientation["rotation_cw"] is None and not orientation["ambiguous"]:
        raise AnalysisValidationError("rotation_cw=null 必須 ambiguous=true")
    evidence = orientation["evidence"]
    if "insufficient_visual_cues" in evidence:
        if (
            len(evidence) != 1
            or orientation["rotation_cw"] is not None
            or not orientation["ambiguous"]
            or orientation["confidence"] > 0.5
        ):
            raise AnalysisValidationError(
                "insufficient_visual_cues 必須獨立、rotation_cw=null、ambiguous=true 且 confidence <= 0.5"
            )
    for key in ("caption", "side_caption"):
        value[key] = value[key].strip()
    return value
