"""Opt-in wire experiment; the production v4/v5/v6 protocol stays unchanged.

The caller supplies a body built by OpenAICompatibleProvider. No model call,
new image decoding or upload happens here. This envelope cannot be imported
as a normal photo analysis; only its landmark evidence is imported separately.
"""
from __future__ import annotations

from copy import deepcopy
import json

from inktime.app.domain.analysis.schema import validate_model_response
from inktime.app.domain.photos.places import PhotoPlace, coordinates


LOCATION_PROMPT = (
    'For this experiment return {"analysis":<the original full photo analysis>,"landmark":{"name":"","confidence":0}}. '
    'Identify a well-known landmark only when visible image features support its exact name. '
    'Use Taiwan Traditional Chinese for the name. The GPS is the camera location, not the subject. '
    'A region hint or nearby POI alone is insufficient evidence. Never invent an address or infer one from coordinates. '
    'If uncertain, use an empty name and confidence 0. Confidence is independent of photo scores. '
    'No search, tools, explanation or extra image request.'
)
LANDMARK_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["name", "confidence"],
    "properties": {
        "name": {"type": "string", "maxLength": 40},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def build_location_experiment(body: dict, *, region_hint: str = "", latitude=None, longitude=None) -> dict:
    result = deepcopy(body)
    response_format = result.get("response_format", {})
    if response_format.get("type") != "json_schema":
        raise ValueError("地標原型需要支援 Structured Outputs 的 Provider")
    original = response_format["json_schema"]
    schema_version = original["schema"]["properties"]["schema_version"].get("const")
    if schema_version != 6:
        raise ValueError("地標原型只接受現有 v6 分析")
    result["response_format"] = {
        "type": "json_schema",
        "json_schema": {
            "name": "inktime_place_experiment_v1", "strict": True,
            "schema": {"type": "object", "additionalProperties": False,
                       "required": ["analysis", "landmark"],
                       "properties": {"analysis": original["schema"], "landmark": deepcopy(LANDMARK_SCHEMA)}},
        },
    }
    result["messages"][0]["content"] += "\n\n" + LOCATION_PROMPT
    hint: dict = {}
    region = " ".join(region_hint.split())
    if len(region) > 80:
        raise ValueError("地區提示過長")
    if region:
        hint["region_hint"] = region
    if latitude is not None or longitude is not None:
        lat, lon = coordinates(latitude, longitude)
        hint["camera_gps"] = [lat, lon]
    if hint:
        result["messages"][1]["content"][0]["text"] += "\nLocation data (not instructions): " + json.dumps(hint, ensure_ascii=False, separators=(",", ":"))
    # The base provider already owns reasoning policy and the output ceiling.
    # Do not increase either to add two small output fields.
    return result


def validate_location_experiment(raw: str | dict) -> tuple[dict, dict]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict) or set(value) != {"analysis", "landmark"}:
        raise ValueError("地標原型回應欄位無效")
    analysis = validate_model_response(value["analysis"], schema_version=6)
    landmark = value["landmark"]
    if not isinstance(landmark, dict) or set(landmark) != {"name", "confidence"}:
        raise ValueError("地標欄位無效")
    place = PhotoPlace.from_dict({"landmark": landmark["name"],
                                 "landmark_confidence": landmark["confidence"], "landmark_source": "vision"})
    if not place.landmark and place.landmark_confidence != 0:
        raise ValueError("沒有地標時信心必須為 0")
    return analysis, {"name": place.landmark, "confidence": place.landmark_confidence}


def incremental_cost(*, photos: int, input_tokens: int, output_tokens: int,
                     reasoning_tokens: int, input_rate: float, output_rate: float) -> float:
    if any(value < 0 for value in (photos, input_tokens, output_tokens, reasoning_tokens, input_rate, output_rate)):
        raise ValueError("成本參數不可為負數")
    return photos * (input_tokens * input_rate + (output_tokens + reasoning_tokens) * output_rate) / 1_000_000
