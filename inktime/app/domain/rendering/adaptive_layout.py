from __future__ import annotations

from datetime import datetime
from math import radians, cos, sin, asin, sqrt
from typing import Any


def dimensions_after_exif(width: int, height: int, orientation: int | None) -> tuple[int, int]:
    return (height, width) if orientation in {5, 6, 7, 8} else (width, height)


def photo_orientation(size: tuple[int, int]) -> str:
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("圖片尺寸必須大於 0")
    aspect_ratio = width / height
    if 0.9 <= aspect_ratio <= 1.1:
        return "square"
    return "landscape" if aspect_ratio > 1 else "portrait"


def pair_orientation(frame_orientation: str) -> str:
    return "portrait" if frame_orientation == "landscape" else "landscape"


def orientation_matches(size: tuple[int, int], desired: str) -> bool:
    return photo_orientation(size) == desired


def _distance_km(first: dict[str, Any], second: dict[str, Any]) -> float | None:
    try:
        lat1, lon1 = float(first["gps_lat"]), float(first["gps_lon"])
        lat2, lon2 = float(second["gps_lat"]), float(second["gps_lon"])
    except (KeyError, TypeError, ValueError):
        return None
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def pair_score(primary: dict[str, Any], candidate: dict[str, Any], *, desired_orientation: str) -> int | None:
    """Score existing metadata only; None means the candidate is not safe to pair."""
    if str(candidate.get("id")) == str(primary.get("id")):
        return None
    if candidate.get("recently_displayed"):
        return None
    duplicate_keys = ("sha256", "duplicate_group_id", "perceptual_hash", "difference_hash")
    if any(primary.get(key) and primary.get(key) == candidate.get(key) for key in duplicate_keys):
        return None
    width, height = dimensions_after_exif(
        int(candidate.get("width") or 0), int(candidate.get("height") or 0), candidate.get("orientation")
    )
    if width <= 0 or height <= 0 or not orientation_matches((width, height), desired_orientation):
        return None
    score = 10  # 方向適合
    related = False
    primary_date = str(primary.get("captured_at") or "")[:10]
    candidate_date = str(candidate.get("captured_at") or "")[:10]
    if primary_date and primary_date == candidate_date:
        score += 30
        related = True
    try:
        delta = abs((datetime.fromisoformat(str(primary["captured_at"]).replace("Z", "+00:00")) - datetime.fromisoformat(str(candidate["captured_at"]).replace("Z", "+00:00"))).total_seconds())
        if delta <= 2 * 3600:
            score += 25
            related = True
    except (KeyError, TypeError, ValueError):
        pass
    if primary.get("city") and str(primary.get("city")).casefold() == str(candidate.get("city") or "").casefold():
        score += 20
        related = True
    elif (distance := _distance_km(primary, candidate)) is not None and distance <= 25:
        score += 20
        related = True
    if set(primary.get("types") or []) & set(candidate.get("types") or []):
        score += 10
        related = True
    if not related:
        return None
    if not candidate.get("ever_displayed"):
        score += 10
    return score


def select_pair_candidate(primary: dict[str, Any], candidates: list[dict[str, Any]], *, frame_orientation: str) -> dict[str, Any] | None:
    desired = pair_orientation(frame_orientation)
    scored = [(pair_score(primary, candidate, desired_orientation=desired), candidate) for candidate in candidates]
    available = [(score, candidate) for score, candidate in scored if score is not None]
    if not available:
        return None
    return max(available, key=lambda item: (item[0], str(item[1].get("captured_at") or ""), str(item[1].get("id"))))[1]
