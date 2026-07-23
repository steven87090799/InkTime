from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import radians, cos, sin, asin, sqrt
from typing import Any


ADAPTIVE_RETAINED_RATIO = 0.78
SUBJECT_RETAINED_RATIO = 0.90


@dataclass(frozen=True)
class AdaptiveDecision:
    mode: str
    retained_ratio: float
    subject_retained_ratio: float


def retained_ratio(source_size: tuple[int, int], target_size: tuple[int, int]) -> float:
    """Original image area retained by a cover crop (after EXIF rotation)."""
    source_width, source_height = source_size
    target_width, target_height = target_size
    if min(source_width, source_height, target_width, target_height) <= 0:
        raise ValueError("圖片尺寸必須大於 0")
    source_aspect = source_width / source_height
    target_aspect = target_width / target_height
    return min(source_aspect / target_aspect, target_aspect / source_aspect, 1.0)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def subject_retained_ratio(
    source_size: tuple[int, int],
    target_size: tuple[int, int],
    subject_box: tuple[float, float, float, float] | None,
    *,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
) -> float:
    """How much of the stored face/subject rectangle survives a focus-aware cover crop."""
    if subject_box is None:
        return 1.0
    left, top, right, bottom = (_clamp(float(value)) for value in subject_box)
    if right <= left or bottom <= top:
        return 1.0
    source_width, source_height = source_size
    target_width, target_height = target_size
    source_aspect = source_width / source_height
    target_aspect = target_width / target_height
    if source_aspect > target_aspect:
        crop_width = target_aspect / source_aspect
        crop_left = max(0.0, min(1.0 - crop_width, _clamp(focus_x) - crop_width / 2))
        crop = (crop_left, 0.0, crop_left + crop_width, 1.0)
    else:
        crop_height = source_aspect / target_aspect
        crop_top = max(0.0, min(1.0 - crop_height, _clamp(focus_y) - crop_height / 2))
        crop = (0.0, crop_top, 1.0, crop_top + crop_height)
    overlap_width = max(0.0, min(right, crop[2]) - max(left, crop[0]))
    overlap_height = max(0.0, min(bottom, crop[3]) - max(top, crop[1]))
    return overlap_width * overlap_height / ((right - left) * (bottom - top))


def decide_adaptive_layout(
    source_size: tuple[int, int],
    photo_area: tuple[int, int],
    *,
    subject_box: tuple[float, float, float, float] | None = None,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
    minimum_retained_ratio: float = ADAPTIVE_RETAINED_RATIO,
) -> AdaptiveDecision:
    kept = retained_ratio(source_size, photo_area)
    subject_kept = subject_retained_ratio(
        source_size, photo_area, subject_box, focus_x=focus_x, focus_y=focus_y
    )
    if kept >= minimum_retained_ratio and subject_kept >= SUBJECT_RETAINED_RATIO:
        return AdaptiveDecision("single", kept, subject_kept)
    return AdaptiveDecision("pair", kept, subject_kept)


def dimensions_after_exif(width: int, height: int, orientation: int | None) -> tuple[int, int]:
    return (height, width) if orientation in {5, 6, 7, 8} else (width, height)


def pair_orientation(frame_orientation: str) -> str:
    return "portrait" if frame_orientation == "landscape" else "landscape"


def orientation_matches(size: tuple[int, int], desired: str) -> bool:
    width, height = size
    return width <= height if desired == "portrait" else width >= height


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
