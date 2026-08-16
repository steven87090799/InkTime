from __future__ import annotations

from datetime import datetime, timezone
from math import radians, cos, sin, asin, sqrt
from typing import Any

from inktime.app.domain.photos.orientation import (
    original_exif_orientation,
    resolve_effective_orientation,
)


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


def effective_dimensions(photo: dict[str, Any]) -> tuple[int, int]:
    """Resolve metadata dimensions through EXIF plus the existing visual override contract."""
    width, height = dimensions_after_exif(
        int(photo.get("width") or 0),
        int(photo.get("height") or 0),
        original_exif_orientation(photo),
    )
    effective = resolve_effective_orientation(
        exif_orientation=original_exif_orientation(photo),
        manual_rotation_cw=photo.get("manual_orientation_rotation_cw"),
        ai_rotation_cw=photo.get("visual_orientation_rotation_cw"),
        ai_confidence=photo.get("visual_orientation_confidence"),
        ai_ambiguous=bool(photo.get("visual_orientation_ambiguous", True)),
    )
    return (height, width) if effective.rotation_degrees in {90, 270} else (width, height)


def _captured(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


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
    """Rank safe metadata-only candidates; relevance is never an acceptance gate."""
    if str(candidate.get("id")) == str(primary.get("id")):
        return None
    if (
        primary.get("library_id")
        and primary.get("library_id") == candidate.get("library_id")
        and primary.get("relative_path")
        and primary.get("relative_path") == candidate.get("relative_path")
    ):
        return None
    duplicate_keys = ("sha256", "duplicate_group_id", "perceptual_hash", "difference_hash")
    if any(primary.get(key) and primary.get(key) == candidate.get(key) for key in duplicate_keys):
        return None
    width, height = effective_dimensions(candidate)
    if width <= 0 or height <= 0 or not orientation_matches((width, height), desired_orientation):
        return None

    primary_captured = _captured(primary.get("captured_at"))
    candidate_captured = _captured(candidate.get("captured_at"))
    delta = (
        abs((primary_captured - candidate_captured).total_seconds())
        if primary_captured is not None and candidate_captured is not None
        else None
    )
    if primary_captured and candidate_captured and primary_captured.date() == candidate_captured.date():
        priority = 4
    elif delta is not None and delta <= 3 * 24 * 3600:
        priority = 3
    elif delta is not None and delta <= 7 * 24 * 3600:
        priority = 2
    else:
        priority = 1

    # Large bands make the four date phases authoritative.  Relevance and
    # display history only order otherwise-safe photos inside one phase.
    score = priority * 1_000_000_000_000_000
    if not candidate.get("recently_displayed"):
        score += 100_000_000_000_000
    if delta is not None:
        score -= int(delta)
    if not candidate.get("ever_displayed"):
        score += 1_000_000
    if (
        primary.get("city")
        and str(primary.get("city")).casefold() == str(candidate.get("city") or "").casefold()
    ):
        score += 200_000
    elif (distance := _distance_km(primary, candidate)) is not None and distance <= 25:
        score += 200_000
    if set(primary.get("types") or []) & set(candidate.get("types") or []):
        score += 100_000
    return score


def select_pair_candidate(
    primary: dict[str, Any], candidates: list[dict[str, Any]], *, frame_orientation: str
) -> dict[str, Any] | None:
    desired = pair_orientation(frame_orientation)
    scored = [
        (pair_score(primary, candidate, desired_orientation=desired), candidate) for candidate in candidates
    ]
    available = [(score, candidate) for score, candidate in scored if score is not None]
    if not available:
        return None
    return max(
        available, key=lambda item: (item[0], str(item[1].get("captured_at") or ""), str(item[1].get("id")))
    )[1]
