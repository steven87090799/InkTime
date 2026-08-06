"""Bounded, deterministic selection that never depends on AI analysis rows."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.domain.photos.quality_policy import evaluate_local_quality
from inktime.app.domain.rendering.contrast_risk import calculate_epaper_contrast_risk


class LocalSelectionPolicy:
    """SQLite-first local candidate ranking and stable two-photo pairing."""

    def __init__(self, database, settings, resilience=None, locations=None) -> None:
        self.database, self.settings, self.resilience, self.locations = (
            database,
            settings,
            resilience,
            locations,
        )

    def _rows(self, *, target: date, limit: int) -> list[dict[str, Any]]:
        month_day = target.strftime("%m-%d")
        with self.database.session() as connection:
            rows = connection.execute(
                """
                SELECT p.*,l.root_path,
                  (SELECT count(*) FROM display_history dh WHERE dh.photo_id=p.id) AS display_count,
                  (SELECT max(displayed_at) FROM display_history dh WHERE dh.photo_id=p.id) AS last_displayed_at
                FROM photos p JOIN libraries l ON l.id=p.library_id
                WHERE p.lifecycle_status='active' AND p.eligible=1 AND l.enabled=1
                  AND p.exclusion_status NOT IN ('auto_excluded','manually_excluded')
                  AND p.local_features_status='complete' AND p.local_candidate_score IS NOT NULL
                ORDER BY CASE WHEN p.captured_month_day=? THEN 0 ELSE 1 END,
                         p.local_candidate_score DESC,p.captured_at ASC,p.id ASC LIMIT ?
                """,
                (month_day, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                if safe_join(Path(str(item["root_path"])), str(item["relative_path"])).is_file():
                    result.append(item)
            except (UnsafePathError, OSError, ValueError):
                continue
        return result

    @staticmethod
    def _hash_distance(first: Any, second: Any) -> int | None:
        """Return a deterministic Hamming distance for local image hashes."""
        left, right = str(first or "").strip().lower(), str(second or "").strip().lower()
        if not left or not right or len(left) != len(right):
            return None
        try:
            return (int(left, 16) ^ int(right, 16)).bit_count()
        except ValueError:
            return 0 if left == right else None

    @staticmethod
    def _coordinate(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _diversity_penalty(cls, candidate: dict[str, Any], selected: list[dict[str, Any]]) -> float:
        """Penalize local duplicates and bursts without introducing an AI call."""
        penalty = 0.0
        candidate_group = str(candidate.get("duplicate_group_id") or candidate.get("burst_group_id") or "")
        candidate_time = str(candidate.get("captured_at") or "")
        try:
            candidate_dt = datetime.fromisoformat(candidate_time.replace("Z", "+00:00"))
        except ValueError:
            candidate_dt = None
        for other in selected:
            other_group = str(other.get("duplicate_group_id") or other.get("burst_group_id") or "")
            if candidate_group and candidate_group == other_group:
                penalty = max(penalty, 18.0)
            candidate_sha = str(candidate.get("sha256") or "").strip().lower()
            other_sha = str(other.get("sha256") or "").strip().lower()
            if candidate_sha and candidate_sha == other_sha:
                penalty = max(penalty, 20.0)
            phash_distance = cls._hash_distance(candidate.get("perceptual_hash"), other.get("perceptual_hash"))
            if phash_distance is not None and phash_distance <= 4:
                penalty = max(penalty, 16.0)
            dhash_distance = cls._hash_distance(candidate.get("difference_hash"), other.get("difference_hash"))
            if dhash_distance is not None and dhash_distance <= 4:
                penalty = max(penalty, 12.0)
            if candidate_dt is not None:
                try:
                    other_dt = datetime.fromisoformat(str(other.get("captured_at") or "").replace("Z", "+00:00"))
                except ValueError:
                    other_dt = None
                if other_dt is not None:
                    if candidate_dt.tzinfo is None and other_dt.tzinfo is not None:
                        candidate_dt = candidate_dt.replace(tzinfo=other_dt.tzinfo)
                    if other_dt.tzinfo is None and candidate_dt.tzinfo is not None:
                        other_dt = other_dt.replace(tzinfo=candidate_dt.tzinfo)
                    if abs((candidate_dt - other_dt).total_seconds()) <= 120:
                        penalty = max(penalty, 6.0)
            candidate_lat = cls._coordinate(candidate.get("gps_lat"))
            other_lat = cls._coordinate(other.get("gps_lat"))
            candidate_lon = cls._coordinate(candidate.get("gps_lon"))
            other_lon = cls._coordinate(other.get("gps_lon"))
            if (
                candidate_lat is None
                or other_lat is None
                or candidate_lon is None
                or other_lon is None
            ):
                continue
            lat_delta = abs(candidate_lat - other_lat)
            lon_delta = abs(candidate_lon - other_lon)
            if lat_delta <= 0.001 and lon_delta <= 0.001:
                penalty = max(penalty, 4.0)
        return penalty

    @staticmethod
    def _diverse_stage_selection(
        rows: list[dict[str, Any]], needed: int, already_selected: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        context = list(already_selected or [])
        remaining = list(rows)
        while remaining and len(selected) < needed:
            ranked = [
                (
                    float(row.get("local_display_score") or 0.0)
                    - LocalSelectionPolicy._diversity_penalty(row, context),
                    -index,
                    str(row["id"]),
                    row,
                )
                for index, row in enumerate(remaining)
            ]
            _, _, _, chosen = max(ranked, key=lambda item: (item[0], item[1], item[2]))
            selected.append(chosen)
            context.append(chosen)
            remaining.remove(chosen)
        return selected

    @staticmethod
    def _recent(value: Any, *, now: datetime) -> tuple[float, float]:
        if not value:
            return 8.0, 0.0
        try:
            seen = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
        except ValueError:
            return 0.0, 0.0
        days = max(0.0, (now - seen).total_seconds() / 86400)
        return (8.0 if days >= 30 else 0.0, 12.0 if days < 7 else 5.0 if days < 30 else 0.0)

    def ranked(
        self,
        *,
        target: date,
        orientation: str,
        limit: int | None = None,
        excluded_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        bound = max(20, min(int(limit or self.settings.get("render.local_candidate_limit", 200)), 1000))
        rows = self._rows(target=target, limit=bound)
        excluded_ids = excluded_ids or set()
        feedback = (
            self.resilience.preference_adjustments(row["id"] for row in rows) if self.resilience else {}
        )
        now = datetime.now(timezone.utc)
        result: list[dict[str, Any]] = []
        for item in rows:
            photo_id = str(item["id"])
            if photo_id in excluded_ids:
                continue
            quality = evaluate_local_quality(item)
            if quality["decision"] == "auto_excluded":
                continue
            not_recent, recent_penalty = self._recent(item.get("last_displayed_at"), now=now)
            try:
                display_count = max(0, int(item.get("display_count") or 0))
            except (TypeError, ValueError):
                display_count = 0
            captured = str(item.get("captured_month_day") or "")
            orientation_fit = (
                3.0
                if (
                    orientation == "landscape" and int(item.get("width") or 0) >= int(item.get("height") or 0)
                )
                or (orientation == "portrait" and int(item.get("height") or 0) >= int(item.get("width") or 0))
                else 0.0
            )
            risk = calculate_epaper_contrast_risk(item)
            components = {
                "base_local_score": float(item.get("local_candidate_score") or 0),
                "favorite_bonus": 8.0 if bool(item.get("favorite")) else 0.0,
                "historical_today_bonus": 24.0 if captured == target.strftime("%m-%d") else 0.0,
                "not_recently_displayed_bonus": not_recent,
                "display_history_count": float(display_count),
                "never_displayed_bonus": 10.0 if display_count == 0 else 0.0,
                "display_count_penalty": -min(display_count, 8) * 1.5,
                "orientation_fit_bonus": orientation_fit,
                "pair_compatibility_bonus": 0.0,
                "manual_feedback_adjustment": float(feedback.get(photo_id, 0.0)),
                "recent_display_penalty": -recent_penalty,
                "low_priority_penalty": -12.0 if quality["decision"] == "low_priority" else 0.0,
                "epaper_risk_penalty": -10.0 if risk == "high" else -4.0 if risk == "medium" else 0.0,
            }
            item["score_components"] = components
            item["local_display_score"] = round(
                sum(value for key, value in components.items() if key != "display_history_count"), 3
            )
            item["epaper_contrast_risk"] = risk
            item["local_quality_decision"] = quality["decision"]
            # The resolver is offline and returns a coarse display city only.
            # A missing resolver/data intentionally remains an unscored location.
            item["city"] = self._location_name(item)
            result.append(item)
        return sorted(
            result,
            key=lambda row: (
                -float(row["local_display_score"]),
                str(row.get("captured_at") or ""),
                str(row["id"]),
            ),
        )

    def _location_name(self, row: dict[str, Any]) -> str:
        if self.locations is None:
            return ""
        try:
            return str(
                self.locations.resolve(
                    row.get("gps_lat"),
                    row.get("gps_lon"),
                    max_distance_km=float(self.settings.get("render.location_max_distance_km", 80)),
                )
                or ""
            ).strip()
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _effective_month_day(target: date, target_month_day: str | None) -> tuple[str, str | None]:
        requested = str(target_month_day or target.strftime("%m-%d"))
        if requested == "02-29" and target.year % 4 != 0:
            return "02-28", "non_leap_year_fallback"
        return requested, None

    @staticmethod
    def _nearby_days(month_day: str, window: int) -> dict[str, int]:
        anchor = date(2000, int(month_day[:2]), int(month_day[3:]))
        values: dict[str, int] = {}
        for offset in range(1, max(0, window) + 1):
            values[(anchor - timedelta(days=offset)).strftime("%m-%d")] = offset
            values[(anchor + timedelta(days=offset)).strftime("%m-%d")] = offset
        return values

    def _pair_score(
        self, primary: dict[str, Any], secondary: dict[str, Any], *, orientation: str
    ) -> tuple[float, dict[str, float]]:
        primary_score = float(primary["local_display_score"])
        secondary_score = float(secondary["local_display_score"])
        primary_landscape = int(primary.get("width") or 0) >= int(primary.get("height") or 0)
        secondary_landscape = int(secondary.get("width") or 0) >= int(secondary.get("height") or 0)
        orientation_bonus = 4.0 if primary_landscape != secondary_landscape else 1.0
        if orientation == "portrait":
            orientation_bonus += 1.0 if primary_landscape or secondary_landscape else 0.0
        primary_city = str(primary.get("city") or "").strip()
        secondary_city = str(secondary.get("city") or "").strip()
        location_bonus = 3.0 if primary_city and primary_city.casefold() == secondary_city.casefold() else 0.0
        p_date, s_date = str(primary.get("captured_date") or ""), str(secondary.get("captured_date") or "")
        proximity = 2.0 if p_date and s_date and p_date[:7] == s_date[:7] else 0.0
        low_penalty = (
            8.0
            if primary.get("local_quality_decision")
            == secondary.get("local_quality_decision")
            == "low_priority"
            else 0.0
        )
        risk_penalty = (
            8.0
            if primary.get("epaper_contrast_risk") == secondary.get("epaper_contrast_risk") == "high"
            else 0.0
        )
        recent_penalty = (
            8.0
            if primary["score_components"]["recent_display_penalty"]
            and secondary["score_components"]["recent_display_penalty"]
            else 0.0
        )
        components = {
            "primary_local_display_score": primary_score,
            "secondary_local_display_score": secondary_score,
            "orientation_compatibility": orientation_bonus,
            "capture_time_proximity": proximity,
            "known_location_match": location_bonus,
            "location_data_available": float(bool(primary_city and secondary_city)),
            "dual_low_priority_penalty": low_penalty,
            "dual_high_epaper_risk_penalty": risk_penalty,
            "dual_recent_display_penalty": recent_penalty,
        }
        return round(
            primary_score
            + secondary_score
            + orientation_bonus
            + proximity
            + location_bonus
            - low_penalty
            - risk_penalty
            - recent_penalty,
            3,
        ), components

    def select(
        self,
        *,
        target: date,
        orientation: str,
        quantity: int,
        layout: str,
        excluded_ids: set[str] | None = None,
        target_month_day: str | None = None,
    ) -> dict[str, Any]:
        ranked = self.ranked(target=target, orientation=orientation, excluded_ids=excluded_ids)
        requested_month_day = str(target_month_day or target.strftime("%m-%d"))
        effective_month_day, leap_reason = self._effective_month_day(target, target_month_day)
        window = int(self.settings.get("render.history_today_window_days", 7))
        fallback = str(self.settings.get("render.history_today_fallback", "nearby_then_ranked"))
        exact = [row for row in ranked if str(row.get("captured_month_day") or "") == effective_month_day]
        nearby_days = self._nearby_days(effective_month_day, window)
        nearby = sorted(
            [row for row in ranked if str(row.get("captured_month_day") or "") in nearby_days],
            key=lambda row: (
                nearby_days[str(row["captured_month_day"])],
                -float(row["local_display_score"]),
                str(row["id"]),
            ),
        )
        exact_ids = {str(row["id"]) for row in exact}
        nearby_ids = {str(row["id"]) for row in nearby}
        ranked_only = [row for row in ranked if str(row["id"]) not in exact_ids | nearby_ids]
        needed = max(1, quantity)
        if fallback == "ranked":
            allowed = list(ranked)
        else:
            # A pool becomes eligible stage-by-stage only when the previous
            # stage cannot satisfy this release.  This keeps an exact pair
            # exact instead of letting Pair Score replace its secondary with
            # an otherwise permitted ranked photo.
            allowed = list(exact)
            if len(allowed) < needed and fallback in {"nearby_only", "nearby_then_ranked"}:
                allowed.extend(nearby)
            if len(allowed) < needed and fallback == "nearby_then_ranked":
                allowed.extend(ranked_only)
        stage_by_id = {
            str(row["id"]): "exact"
            if str(row["id"]) in exact_ids
            else "nearby"
            if str(row["id"]) in nearby_ids
            else "ranked"
            for row in allowed
        }
        selected: list[dict[str, Any]] = []
        for stage in ("exact", "nearby", "ranked"):
            if len(selected) >= needed:
                break
            stage_rows = [row for row in allowed if stage_by_id[str(row["id"])] == stage]
            selected.extend(self._diverse_stage_selection(stage_rows, needed - len(selected), selected))
        pair_candidate_count = 0
        if layout in {"photo_pair", "photo_pair_caption"} and selected:
            primary = selected[0]
            peers = [row for row in allowed[:50] if str(row["id"]) != str(primary["id"])]
            pair_candidate_count = len(peers)
            if peers:
                pairs = [
                    (
                        self._pair_score(primary, peer, orientation=orientation),
                        self._diversity_penalty(peer, [primary]),
                        peer,
                    )
                    for peer in peers
                ]
                (pair_score, pair_components), diversity_penalty, secondary = sorted(
                    pairs, key=lambda item: (-(item[0][0] - item[1]), str(item[2]["id"]))
                )[0]
                pair_components["diversity_penalty"] = -diversity_penalty
                pair_score = round(pair_score - diversity_penalty, 3)
                primary["score_components"]["pair_compatibility_bonus"] = pair_components[
                    "orientation_compatibility"
                ]
                primary["pair_score"] = pair_score
                primary["pair_score_components"] = pair_components
                selected = [primary, secondary] + selected[2:]
        selected = selected[:needed]
        selected_stages = [stage_by_id[str(row["id"])] for row in selected]
        fallback_type = (
            "none"
            if not selected
            else "ranked_fallback"
            if "ranked" in selected_stages
            else "nearby_day"
            if "nearby" in selected_stages
            else "exact_day"
        )
        trace_id = None
        if self.resilience is not None:
            selected_ids = {str(row["id"]) for row in selected}
            trace_candidates: list[dict[str, Any]] = []
            for row in allowed[:50]:
                candidate = {
                    "photo_id": row["id"],
                    "base_score": row.get("local_candidate_score"),
                    "adjusted_score": row["local_display_score"],
                    "selected": str(row["id"]) in selected_ids,
                    "score_components": {
                        **row["score_components"],
                        "candidate_stage": stage_by_id[str(row["id"])],
                        "selected_role": "primary"
                        if selected and str(row["id"]) == str(selected[0]["id"])
                        else "secondary"
                        if len(selected) > 1 and str(row["id"]) == str(selected[1]["id"])
                        else "not_selected",
                    },
                }
                if (
                    selected
                    and len(selected) > 1
                    and str(row["id"]) == str(selected[0]["id"])
                    and "pair_score" in row
                ):
                    candidate["score_components"].update(
                        pair_score=row["pair_score"],
                        pair_score_components=row["pair_score_components"],
                        paired_secondary_photo_id=str(selected[1]["id"]),
                    )
                trace_candidates.append(candidate)
            algorithm = self.resilience.algorithm_version(
                name="local_display_selection",
                version="v1",
                configuration={"candidate_limit": min(len(ranked), 200), "fallback_setting": fallback},
                renderer="server",
                layout=layout,
                pairing="local-v1",
                scoring="local-display-v1",
            )
            trace_id = self.resilience.create_trace(
                execution_mode="production",
                algorithm_version_id=algorithm,
                primary_photo_id=str(selected[0]["id"]) if selected else None,
                secondary_photo_id=str(selected[1]["id"])
                if len(selected) > 1 and layout in {"photo_pair", "photo_pair_caption"}
                else None,
                layout_mode=layout,
                candidates=trace_candidates,
                candidate_count=len(allowed),
                eligible_count=len(allowed),
                reasons=[fallback_type],
                context={
                    "target_date": target.isoformat(),
                    "requested_month_day": requested_month_day,
                    "effective_month_day": effective_month_day,
                    "fallback_type": fallback_type,
                    "fallback_reason": leap_reason,
                    "fallback_setting": fallback,
                    "window_days": window,
                    "exact_count": len(exact),
                    "nearby_count": len(nearby),
                    "ranked_count": len(ranked_only),
                    "allowed_pool_count": len(allowed),
                    "pair_candidate_count": pair_candidate_count,
                    "primary_selection_stage": selected_stages[0] if selected_stages else None,
                    "secondary_selection_stage": selected_stages[1] if len(selected_stages) > 1 else None,
                    "selection_stages": {str(row["id"]): stage_by_id[str(row["id"])] for row in allowed[:50]},
                    "selection_mode": "local_only",
                },
            )
        return {
            "candidates": ranked,
            "selected": selected,
            "fallback": fallback_type,
            "allowed_pool": allowed,
            "exact_pool": exact,
            "nearby_pool": nearby,
            "ranked_pool": ranked_only,
            "decision_trace_id": trace_id,
        }
