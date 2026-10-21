"""Address evidence and visual landmarks have independent sources and confidence."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

from inktime.app.domain.analysis.traditional_chinese import to_taiwan_traditional


PLACE_VERSION = "photo-place-v1"
LANDMARK_MIN_CONFIDENCE = 0.90


def coordinates(latitude: Any, longitude: Any) -> tuple[float, float]:
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        raise ValueError("GPS 必須是有限數值")
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("GPS 必須是有限數值") from exc
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("GPS 超出範圍")
    return lat, lon


def coordinate_key(latitude: Any, longitude: Any) -> str:
    if latitude is None and longitude is None:
        return "none"
    lat, lon = coordinates(latitude, longitude)
    # Preserve the exact input point. A rounded spatial cell must never be
    # reused as a house address across a road or administrative boundary.
    return f"{lat or 0.0!r},{lon or 0.0!r}"


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("地點文字必須是字串")
    value = " ".join(value.split())
    if len(value) > maximum:
        raise ValueError("地點文字過長")
    return to_taiwan_traditional(value)


@dataclass(frozen=True)
class PhotoPlace:
    full_address: str = ""
    district: str = ""
    city: str = ""
    region: str = ""
    country: str = ""
    source: str = ""
    attribution: str = ""
    precision: str = "unknown"
    landmark: str = ""
    landmark_confidence: float = 0.0
    landmark_source: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> PhotoPlace:
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("地點資料欄位無效")
        fields: dict[str, Any] = {key: _text(item, 500 if key == "full_address" else 120)
                  for key, item in value.items() if key != "landmark_confidence"}
        confidence = value.get("landmark_confidence", 0.0)
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            raise ValueError("地標信心必須介於 0 與 1")
        if len(fields.get("landmark", "")) > 40:
            raise ValueError("地標名稱過長")
        return cls(**fields, landmark_confidence=float(confidence))

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def administrative_label(self) -> str:
        return self.district or self.city or self.region or self.country

    @property
    def display_label(self) -> str:
        if self.landmark and self.landmark_source == "vision" and self.landmark_confidence >= LANDMARK_MIN_CONFIDENCE:
            return self.landmark
        return self.administrative_label


def from_geoapify(payload: dict) -> PhotoPlace:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ValueError("反向地理編碼回應無效")
    if not results:
        return PhotoPlace(source="geoapify", precision="unavailable")
    result = results[0]
    if not isinstance(result, dict):
        raise ValueError("反向地理編碼回應無效")
    # A nearby amenity name is not evidence of a famous landmark in the image.
    # For Taiwan, district is an administrative unit; suburb may be a smaller
    # neighbourhood and must not take precedence over a supplied district.
    return PhotoPlace.from_dict({
        "full_address": result.get("formatted", ""),
        "district": result.get("district", "") or result.get("suburb", ""),
        "city": result.get("city", "") or result.get("town", "") or result.get("village", ""),
        "region": result.get("state", "") or result.get("county", ""),
        "country": result.get("country", ""),
        "source": "geoapify",
        "attribution": "Powered by Geoapify · © OpenStreetMap contributors",
        "precision": result.get("result_type", "unknown"),
    })


def place_date_label(place: str, date: str) -> str:
    return "・".join(value for value in (place, date) if value)
