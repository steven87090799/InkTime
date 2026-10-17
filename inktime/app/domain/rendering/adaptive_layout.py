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


def auto_photo_layout(size: tuple[int, int], frame_orientation: str) -> str:
    if frame_orientation not in {"portrait", "landscape"}:
        raise ValueError("相框方向不合法")
    orientation = photo_orientation(size)
    return "full" if orientation in {"square", frame_orientation} else "photo_pair"


class InsufficientPhotoContentError(ValueError):
    """A deterministic shortage of sources, not a retryable render failure."""

    code = "NO_ELIGIBLE_CANDIDATES"


def select_auto_photo_frames(
    candidates: list[dict[str, Any]], *, frame_orientation: str, count: int
) -> list[list[dict[str, Any]]]:
    """Reserve distinct sources: matching photos alone, opposite photos in pairs."""
    unique = list({str(row["id"]): row for row in candidates}.values())
    available = list(unique)
    frames: list[list[dict[str, Any]]] = []
    while available and len(frames) < count:
        primary = available.pop(0)
        size = effective_dimensions(primary)
        if min(size) <= 0:
            continue
        if auto_photo_layout(size, frame_orientation) == "full":
            frames.append([primary])
            continue
        secondary = select_pair_candidate(primary, available, frame_orientation=frame_orientation)
        if secondary is not None:
            available.remove(secondary)
            frames.append([primary, secondary])
    if len(frames) >= count:
        return frames

    # Keep the preferred greedy assignment whenever it fills the request.
    # Otherwise find augmenting paths (including odd cycles) before declaring
    # a shortage: a locally attractive pair can strand two compatible peers.
    singles: list[list[dict[str, Any]]] = []
    peers: list[dict[str, Any]] = []
    for row in unique:
        size = effective_dimensions(row)
        if min(size) <= 0:
            continue
        if auto_photo_layout(size, frame_orientation) == "full":
            singles.append([row])
        else:
            peers.append(row)
    desired = pair_orientation(frame_orientation)
    edges: list[list[int]] = []
    for primary in peers:
        scored = [(pair_score(primary, row, desired_orientation=desired), i) for i, row in enumerate(peers)]
        valid = [(score, i) for score, i in scored if score is not None]
        valid.sort(key=lambda item: (
            not peers[item[1]].get("recently_displayed"), item[0],
            str(peers[item[1]].get("captured_at") or ""), str(peers[item[1]]["id"]),
        ), reverse=True)
        edges.append([i for _, i in valid])
    matching = _maximum_pairing(edges, count)
    complete = singles + [[peers[i], peers[j]] for i, j in enumerate(matching) if j > i]
    order = {str(row["id"]): i for i, row in enumerate(unique)}
    complete.sort(key=lambda group: order[str(group[0]["id"])])
    return complete[:count]


def _maximum_pairing(edges: list[list[int]], limit: int) -> list[int]:
    """Maximum cardinality matching in a general graph, bounded by frame count.

    Edmonds' alternating-tree search contracts odd cycles instead of treating
    this symmetric photo compatibility graph as bipartite. Neighbor order
    preserves preference when several augmenting paths are possible.
    """
    size = len(edges)
    match = [-1] * size
    pairs = 0
    for root in range(size):
        if pairs >= limit:
            break
        if match[root] != -1:
            continue
        parent, base = [-1] * size, list(range(size))
        used = [False] * size
        used[root] = True
        queue = [root]

        def ancestor(first: int, second: int, *, base=base, parent=parent) -> int:
            seen: set[int] = set()
            while True:
                first = base[first]
                seen.add(first)
                if match[first] == -1:
                    break
                first = parent[match[first]]
            while base[second] not in seen:
                second = parent[match[base[second]]]
            return base[second]

        def mark_path(vertex: int, common: int, child: int, blossom: set[int], *, base=base, parent=parent) -> None:
            while base[vertex] != common:
                blossom.update((base[vertex], base[match[vertex]]))
                parent[vertex] = child
                child = match[vertex]
                vertex = parent[match[vertex]]

        endpoint = -1
        for vertex in queue:
            for peer in edges[vertex]:
                if base[vertex] == base[peer] or match[vertex] == peer:
                    continue
                if peer == root or (match[peer] != -1 and parent[match[peer]] != -1):
                    common = ancestor(vertex, peer)
                    blossom: set[int] = set()
                    mark_path(vertex, common, peer, blossom)
                    mark_path(peer, common, vertex, blossom)
                    for i in range(size):
                        if base[i] in blossom:
                            base[i] = common
                            if not used[i]:
                                used[i] = True
                                queue.append(i)
                elif parent[peer] == -1:
                    parent[peer] = vertex
                    if match[peer] == -1:
                        endpoint = peer
                        break
                    partner = match[peer]
                    used[partner] = True
                    queue.append(partner)
            if endpoint != -1:
                break
        if endpoint == -1:
            continue
        pairs += 1
        while endpoint != -1:
            previous = parent[endpoint]
            following = match[previous] if previous != -1 else -1
            match[endpoint] = previous
            if previous != -1:
                match[previous] = endpoint
            endpoint = following
    return match


def effective_dimensions(photo: dict[str, Any]) -> tuple[int, int]:
    """Resolve metadata dimensions through EXIF plus the existing visual override contract."""
    # Stored scanner dimensions already describe EXIF-transposed pixels.
    width, height = int(photo.get("width") or 0), int(photo.get("height") or 0)
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

    # Large bands make the four date phases authoritative inside one
    # freshness tier.  Fresh candidates are separated before this score is
    # compared, so a recently displayed same-day photo cannot beat any safe
    # fresh candidate.
    score = priority * 1_000_000_000_000_000
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
    fresh = [item for item in available if not item[1].get("recently_displayed")]
    pool = fresh or available
    return max(
        pool, key=lambda item: (item[0], str(item[1].get("captured_at") or ""), str(item[1].get("id")))
    )[1]
