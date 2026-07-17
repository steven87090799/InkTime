from __future__ import annotations

import json
import math
from typing import Any


ALLOWED_TYPES = {
    "人物", "孩子", "家庭", "旅行", "風景", "美食", "寵物", "貓咪",
    "日常", "活動", "建築", "夜景", "植物", "文件", "收據", "截圖", "雜物", "其他",
}
REQUIRED_FIELDS = {
    "schema_version", "caption", "types", "memory_score", "beauty_score",
    "technical_quality_score", "emotion_score", "side_caption", "should_keep",
    "sensitive", "reason",
}


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
            "types": {"type": "array", "items": {"type": "string", "enum": sorted(ALLOWED_TYPES)}, "minItems": 1, "uniqueItems": True},
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
    if set(value) != REQUIRED_FIELDS:
        missing = sorted(REQUIRED_FIELDS - set(value))
        extra = sorted(set(value) - REQUIRED_FIELDS)
        raise AnalysisValidationError(f"欄位不符合 Schema；缺少={missing}，多餘={extra}")
    if value["schema_version"] != 1:
        raise AnalysisValidationError("不支援的 schema_version")
    if not isinstance(value["caption"], str) or not value["caption"].strip():
        raise AnalysisValidationError("caption 不可空白")
    if not isinstance(value["side_caption"], str) or len(value["side_caption"]) > 120:
        raise AnalysisValidationError("side_caption 格式不合法")
    if not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 240:
        raise AnalysisValidationError("reason 格式不合法")
    types = value["types"]
    if not isinstance(types, list) or not types or len(types) != len(set(types)) or any(item not in ALLOWED_TYPES for item in types):
        raise AnalysisValidationError("types 含有不允許或重複的類型")
    if not isinstance(value["should_keep"], bool) or not isinstance(value["sensitive"], bool):
        raise AnalysisValidationError("布林欄位格式不合法")
    for field in ("memory_score", "beauty_score", "technical_quality_score", "emotion_score"):
        value[field] = _score(value[field], field)
    value["caption"] = value["caption"].strip()
    value["side_caption"] = value["side_caption"].strip()
    value["reason"] = value["reason"].strip()
    return value
