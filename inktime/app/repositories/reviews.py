"""Keyset-paginated review workbench persistence.

The workbench never exposes source paths or original files.  It returns a
bounded metadata projection and uses optimistic versions for all operator
writes so a stale browser cannot overwrite a newer review.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from typing import Any

from inktime.app.db import Database


REVIEW_STATES = {"unreviewed", "keep", "exclude", "needs_review"}
_MAX_PAGE = 80
LOW_CONFIDENCE_THRESHOLD = 0.6


class ReviewConflictError(RuntimeError):
    code = "REVIEW-409"

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("Review 資料已由另一個工作階段更新")
        self.current = current


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _cursor_encode(taken_at: str, photo_id: str) -> str:
    raw = _json({"taken_at": taken_at, "id": photo_id}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_decode(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    try:
        padded = str(value) + "=" * (-len(str(value)) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        taken_at, photo_id = str(decoded["taken_at"]), str(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ValueError("REVIEW-001 cursor 格式錯誤") from None
    if not taken_at or not photo_id or len(taken_at) > 80 or len(photo_id) > 200:
        raise ValueError("REVIEW-001 cursor 格式錯誤")
    return taken_at, photo_id


class ReviewRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _where(filters: dict[str, Any]) -> tuple[list[str], list[Any]]:
        clauses = ["p.lifecycle_status NOT IN ('deleted','archived')"]
        params: list[Any] = []
        query = str(filters.get("q") or "").strip()
        if query:
            like = f"%{_like(query[:120])}%"
            clauses.append(
                "(p.relative_path LIKE ? ESCAPE '\\' OR COALESCE(a.caption,'') LIKE ? ESCAPE '\\' "
                "OR COALESCE(a.side_caption,'') LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like])
        state = str(filters.get("review_state") or filters.get("review_status") or "").strip()
        state = {"accepted": "keep", "rejected": "exclude", "kept": "keep"}.get(state, state)
        if state:
            if state not in REVIEW_STATES:
                raise ValueError("REVIEW-001 review_state 不合法")
            clauses.append(
                "CASE WHEN decision.id IS NOT NULL THEN decision.review_state "
                "ELSE COALESCE(feedback.review_state,'unreviewed') END=?"
            )
            params.append(state)
        if filters.get("candidate_pool") is not None:
            clauses.append(
                "CASE WHEN decision.id IS NOT NULL THEN decision.candidate_pool "
                "ELSE COALESCE(feedback.candidate_pool,0) END=?"
            )
            params.append(int(bool(filters["candidate_pool"])))
        if filters.get("favorite") is not None:
            clauses.append("p.favorite=?")
            params.append(int(bool(filters["favorite"])))
        date_value = str(filters.get("date") or "").strip()
        if date_value:
            clauses.append("substr(COALESCE(p.review_taken_at,p.captured_at,p.created_at),1,10)=?")
            params.append(date_value[:10])
        month_day = str(filters.get("month_day") or "").strip()
        if month_day:
            clauses.append("p.captured_month_day=?")
            params.append(month_day[:5])
        year = str(filters.get("year") or "").strip()
        if year:
            clauses.append("substr(COALESCE(p.review_taken_at,p.captured_at,p.created_at),1,4)=?")
            params.append(year[:4])
        month = str(filters.get("month") or "").strip()
        if month:
            clauses.append("substr(COALESCE(p.review_taken_at,p.captured_at,p.created_at),6,2)=?")
            params.append(f"{int(month[:2]):02d}")
        day = str(filters.get("day") or "").strip()
        if day:
            clauses.append("substr(COALESCE(p.review_taken_at,p.captured_at,p.created_at),9,2)=?")
            params.append(f"{int(day[:2]):02d}")
        reason = str(filters.get("reason") or "").strip()
        if reason:
            clauses.append("(COALESCE(p.reject_reason,'')=? OR COALESCE(p.reject_rule,'')=? OR COALESCE(p.exclusion_status,'')=?)")
            params.extend([reason[:100], reason[:100], reason[:100]])
        provider = str(filters.get("provider") or "").strip()
        if provider:
            clauses.append("a.provider=?")
            params.append(provider[:120])
        model = str(filters.get("model") or "").strip()
        if model:
            clauses.append("a.model=?")
            params.append(model[:200])
        library_id = str(filters.get("library_id") or "").strip()
        if library_id:
            clauses.append("p.library_id=?")
            params.append(library_id[:200])
        category = str(filters.get("category") or "").strip()
        if category:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(COALESCE(a.types_json,'[]')) WHERE json_each.value=?)"
            )
            params.append(category[:80])
        ai_status = str(filters.get("ai_status") or "").strip()
        if ai_status == "completed":
            clauses.append("a.id IS NOT NULL")
        elif ai_status == "missing":
            clauses.append("a.id IS NULL")
        elif ai_status:
            raise ValueError("REVIEW-001 ai_status 不合法")
        if filters.get("content_excluded") is not None:
            clauses.append("(p.eligible=0 AND p.reject_rule='content-filter')=?")
            params.append(int(bool(filters["content_excluded"])))
        if filters.get("excluded") is not None:
            clauses.append("p.eligible=?")
            params.append(0 if bool(filters["excluded"]) else 1)
        score_min = filters.get("score_min")
        if score_min not in {None, ""}:
            clauses.append("COALESCE(a.ranking_score,0)>=?")
            params.append(float(str(score_min)))
        score_max = filters.get("score_max")
        if score_max not in {None, ""}:
            clauses.append("COALESCE(a.ranking_score,0)<=?")
            params.append(float(str(score_max)))
        confidence = "json_extract(a.content_filter_json,'$.confidence')"
        if filters.get("low_confidence"):
            clauses.append(f"{confidence} IS NOT NULL AND CAST({confidence} AS REAL)<?")
            params.append(LOW_CONFIDENCE_THRESHOLD)
        for key in ("understanding_incorrect", "caption_bad", "scores_unreasonable"):
            if filters.get(key) is not None:
                clauses.append(f"COALESCE(feedback.{key},decision.{key},0)=?")
                params.append(int(bool(filters[key])))
        return clauses, params

    @staticmethod
    def _projection() -> str:
        return """
            WITH latest_analysis AS (
                SELECT pa.*
                FROM photo_analysis pa
                JOIN (SELECT photo_id,MAX(id) AS id FROM photo_analysis GROUP BY photo_id) latest
                  ON latest.id=pa.id
            )
            SELECT p.id,p.library_id,p.width,p.height,p.format,p.sha256,p.favorite,p.eligible,
                   p.exclusion_status,p.reject_reason,p.reject_rule,p.reject_details_json,
                   p.lifecycle_status,p.captured_at,p.captured_date,p.captured_month_day,
                   COALESCE(p.review_taken_at,p.captured_at,p.created_at) AS review_taken_at,
                   COALESCE(p.review_date_source,CASE WHEN p.captured_at IS NULL THEN 'created_at' ELSE 'captured_at' END) AS review_date_source,
                   COALESCE(feedback.analysis_id,decision.analysis_id) AS review_analysis_id,
                   CASE WHEN decision.id IS NOT NULL THEN decision.review_state ELSE COALESCE(feedback.review_state,'unreviewed') END AS review_state,
                   CASE WHEN decision.id IS NOT NULL THEN decision.caption_override ELSE feedback.caption_override END AS caption_override,
                   CASE WHEN decision.id IS NOT NULL THEN decision.candidate_pool ELSE COALESCE(feedback.candidate_pool,0) END AS candidate_pool,
                   CASE WHEN decision.id IS NOT NULL THEN decision.note ELSE feedback.note END AS note,
                   COALESCE(feedback.understanding_incorrect,decision.understanding_incorrect,0) AS understanding_incorrect,
                   COALESCE(feedback.caption_bad,decision.caption_bad,0) AS caption_bad,
                   COALESCE(feedback.scores_unreasonable,decision.scores_unreasonable,0) AS scores_unreasonable,
                   CASE WHEN decision.id IS NOT NULL THEN decision.accepted_at ELSE feedback.accepted_at END AS accepted_at,
                   CASE WHEN decision.id IS NOT NULL THEN decision.version ELSE COALESCE(feedback.version,0) END AS review_version,
                   CASE WHEN decision.id IS NOT NULL THEN decision.updated_at ELSE feedback.updated_at END AS review_updated_at,
                   CASE WHEN decision.id IS NOT NULL THEN decision.updated_by ELSE feedback.updated_by END AS review_updated_by,
                   a.id AS analysis_id,a.schema_version AS analysis_schema_version,a.stage AS analysis_stage,
                   a.provider AS analysis_provider,a.model AS analysis_model,a.prompt_version,
                   a.analysis_fingerprint,a.created_at AS analysis_created_at,a.caption,a.side_caption,
                   a.types_json,a.memory_score,a.visual_score,a.local_quality_score,
                   a.ranking_score,a.local_score,a.final_ranking_score,a.semantic_json,
                   p.visual_orientation_rotation_cw,p.visual_orientation_confidence,
                   p.visual_orientation_ambiguous,p.visual_orientation_evidence_json
            FROM photos p
            LEFT JOIN latest_analysis a ON a.photo_id=p.id
            LEFT JOIN photo_reviews decision ON decision.photo_id=p.id AND decision.analysis_id IS NULL
            LEFT JOIN photo_reviews feedback ON feedback.photo_id=p.id AND feedback.analysis_id=a.id
        """

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        value = dict(row)
        for key in ("types_json", "reject_details_json", "visual_orientation_evidence_json"):
            raw = value.pop(key, None)
            if raw:
                try:
                    value[key.removesuffix("_json")] = json.loads(str(raw))
                except (TypeError, ValueError, json.JSONDecodeError):
                    value[key.removesuffix("_json")] = {} if key != "types_json" else []
            else:
                value[key.removesuffix("_json")] = [] if key == "types_json" else {}
        semantic_raw = value.pop("semantic_json", None)
        if semantic_raw:
            try:
                semantic = json.loads(str(semantic_raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                semantic = {}
        else:
            semantic = {}
        value["confidence"] = ((semantic.get("values") or {}).get("content_filter") or {}).get("confidence")
        value["analysis_details"] = semantic.get("values") if isinstance(semantic.get("values"), dict) else {}
        confidence = value.get("confidence")
        try:
            value["low_confidence"] = confidence is not None and float(confidence) < LOW_CONFIDENCE_THRESHOLD
        except (TypeError, ValueError):
            value["low_confidence"] = False
        value["favorite"] = bool(value.get("favorite"))
        value["eligible"] = bool(value.get("eligible"))
        value["candidate_pool"] = bool(value.get("candidate_pool"))
        value["review_version"] = int(value.get("review_version") or 0)
        value["review_state"] = value.get("review_state") or "unreviewed"
        value["review_status"] = value["review_state"]
        value["human_side_caption"] = value.get("caption_override")
        for key in ("understanding_incorrect", "caption_bad", "scores_unreasonable"):
            value[key] = bool(value.get(key))
        value["thumbnail_url"] = f"/api/v1/review/photos/{value['id']}/thumbnail"
        return value

    def list_photos(self, *, filters: dict[str, Any] | None = None, cursor: str | None = None, limit: int = 40) -> dict[str, Any]:
        filters = dict(filters or {})
        limit = max(1, min(int(limit), _MAX_PAGE))
        clauses, params = self._where(filters)
        decoded = _cursor_decode(cursor)
        if decoded:
            taken_at, photo_id = decoded
            clauses.append("(COALESCE(p.review_taken_at,p.captured_at,p.created_at)<? OR (COALESCE(p.review_taken_at,p.captured_at,p.created_at)=? AND p.id<?))")
            params.extend([taken_at, taken_at, photo_id])
        where = " AND ".join(clauses)
        with self.database.session() as connection:
            rows = connection.execute(
                f"{self._projection()} WHERE {where} ORDER BY review_taken_at DESC,p.id DESC LIMIT ?",  # noqa: S608 -- clauses are fixed above
                params + [limit + 1],
            ).fetchall()
        has_more = len(rows) > limit
        visible = [self._row(row) for row in (rows[:limit] if has_more else rows)]
        next_cursor = _cursor_encode(str(visible[-1]["review_taken_at"]), str(visible[-1]["id"])) if has_more and visible else None
        return {"items": visible, "next_cursor": next_cursor, "has_more": has_more, "limit": limit}

    def summary(self, *, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = dict(filters or {})
        clauses, params = self._where(filters)
        where = " AND ".join(clauses)
        with self.database.session() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM photos p "  # noqa: S608 -- clauses are fixed above
                    "LEFT JOIN photo_analysis a ON a.id=(SELECT MAX(id) FROM photo_analysis WHERE photo_id=p.id) "
                    "LEFT JOIN photo_reviews decision ON decision.photo_id=p.id AND decision.analysis_id IS NULL "
                    "LEFT JOIN photo_reviews feedback ON feedback.photo_id=p.id AND feedback.analysis_id=a.id "
                    f"WHERE {where}",
                    params,
                ).fetchone()[0]
            )
            states = connection.execute(
                "SELECT CASE WHEN decision.id IS NOT NULL THEN decision.review_state "  # noqa: S608 -- clauses are fixed above
                "ELSE COALESCE(feedback.review_state,'unreviewed') END AS state,COUNT(*) AS count FROM photos p "  # noqa: S608 -- clauses are fixed above
                "LEFT JOIN photo_analysis a ON a.id=(SELECT MAX(id) FROM photo_analysis WHERE photo_id=p.id) "
                "LEFT JOIN photo_reviews decision ON decision.photo_id=p.id AND decision.analysis_id IS NULL "
                "LEFT JOIN photo_reviews feedback ON feedback.photo_id=p.id AND feedback.analysis_id=a.id "
                f"WHERE {where} GROUP BY state",  # noqa: S608 -- clauses are fixed above
                params,
            ).fetchall()
            feedback = connection.execute(
                f"SELECT "  # noqa: S608 -- clauses are fixed above
                "COALESCE(SUM(CASE WHEN COALESCE(feedback.understanding_incorrect,decision.understanding_incorrect,0)=1 THEN 1 ELSE 0 END),0) AS understanding_incorrect,"
                "COALESCE(SUM(CASE WHEN COALESCE(feedback.caption_bad,decision.caption_bad,0)=1 THEN 1 ELSE 0 END),0) AS caption_bad,"
                "COALESCE(SUM(CASE WHEN COALESCE(feedback.scores_unreasonable,decision.scores_unreasonable,0)=1 THEN 1 ELSE 0 END),0) AS scores_unreasonable,"
                "COALESCE(SUM(CASE WHEN p.eligible=0 AND p.reject_rule='content-filter' THEN 1 ELSE 0 END),0) AS content_excluded,"
                "COALESCE(SUM(CASE WHEN COALESCE(CASE WHEN decision.id IS NOT NULL THEN decision.candidate_pool ELSE feedback.candidate_pool END,0)=1 THEN 1 ELSE 0 END),0) AS candidate_pool "
                "FROM photos p "
                "LEFT JOIN photo_analysis a ON a.id=(SELECT MAX(id) FROM photo_analysis WHERE photo_id=p.id) "
                "LEFT JOIN photo_reviews decision ON decision.photo_id=p.id AND decision.analysis_id IS NULL "
                "LEFT JOIN photo_reviews feedback ON feedback.photo_id=p.id AND feedback.analysis_id=a.id "
                f"WHERE {where}",
                params,
            ).fetchone()
            years = connection.execute("SELECT substr(COALESCE(review_taken_at,captured_at,created_at),1,4) AS year,COUNT(*) AS count FROM photos WHERE lifecycle_status NOT IN ('deleted','archived') GROUP BY year ORDER BY year DESC LIMIT 30").fetchall()
            reasons = connection.execute("SELECT COALESCE(reject_reason,reject_rule,exclusion_status,'unknown') AS reason,COUNT(*) AS count FROM photos WHERE lifecycle_status NOT IN ('deleted','archived') GROUP BY reason ORDER BY count DESC,reason LIMIT 50").fetchall()
        return {
            "total": total,
            "states": {str(row["state"]): int(row["count"]) for row in states},
            "feedback": {
                key: int(feedback[key])
                for key in (
                    "understanding_incorrect",
                    "caption_bad",
                    "scores_unreasonable",
                    "content_excluded",
                    "candidate_pool",
                )
            },
            "years": [{"value": row["year"], "count": int(row["count"])} for row in years if row["year"]],
            "reasons": [{"value": row["reason"], "count": int(row["count"])} for row in reasons],
        }

    def date_facets(self, *, filters: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        filters = dict(filters or {})
        clauses, params = self._where(filters)
        where = " AND ".join(clauses)
        base = (
            " FROM photos p "
            "LEFT JOIN photo_analysis a ON a.id=(SELECT MAX(id) FROM photo_analysis WHERE photo_id=p.id) "
            "LEFT JOIN photo_reviews decision ON decision.photo_id=p.id AND decision.analysis_id IS NULL "
            "LEFT JOIN photo_reviews feedback ON feedback.photo_id=p.id AND feedback.analysis_id=a.id "
        )
        date_expr = "COALESCE(p.review_taken_at,p.captured_at,p.created_at)"
        with self.database.session() as connection:
            years = connection.execute(
                f"SELECT substr({date_expr},1,4) AS value,COUNT(*) AS count{base}WHERE {where} GROUP BY value ORDER BY value DESC LIMIT 30",
                params,
            ).fetchall()
            months = connection.execute(
                f"SELECT substr({date_expr},6,2) AS value,COUNT(*) AS count{base}WHERE {where} GROUP BY value ORDER BY value LIMIT 12",
                params,
            ).fetchall()
            days = connection.execute(
                f"SELECT substr({date_expr},9,2) AS value,COUNT(*) AS count{base}WHERE {where} GROUP BY value ORDER BY value LIMIT 31",
                params,
            ).fetchall()
        def convert(rows: Any) -> list[dict[str, Any]]:
            return [{"value": str(row["value"]), "count": int(row["count"])} for row in rows if row["value"]]

        return {"years": convert(years), "months": convert(months), "days": convert(days)}

    def get(self, photo_id: str) -> dict[str, Any] | None:
        result = self.list_photos(filters={"q": photo_id}, limit=1)
        for item in result["items"]:
            if item["id"] == photo_id:
                return item
        with self.database.session() as connection:
            row = connection.execute(
                f"{self._projection()} WHERE p.id=? AND p.lifecycle_status NOT IN ('deleted','archived')",
                (photo_id,),
            ).fetchone()  # noqa: S608
        return self._row(row) if row else None

    def update(self, photo_id: str, payload: dict[str, Any], *, actor_id: str, expected_version: int) -> dict[str, Any]:
        unknown = set(payload) - {
            "review_state",
            "caption_override",
            "candidate_pool",
            "note",
            "favorite",
            "understanding_incorrect",
            "caption_bad",
            "scores_unreasonable",
            "analysis_id",
            "review_status",
            "side_caption",
            "human_side_caption",
        }
        if unknown:
            raise ValueError("REVIEW-001 不支援的欄位")
        state_value = str(payload.get("review_state", payload.get("review_status", ""))).strip()
        state = {"accepted": "keep", "rejected": "exclude", "kept": "keep"}.get(state_value, state_value) or None
        if state is not None and state not in REVIEW_STATES:
            raise ValueError("REVIEW-001 review_state 不合法")
        caption_key = (
            "caption_override" if "caption_override" in payload
            else "side_caption" if "side_caption" in payload
            else "human_side_caption" if "human_side_caption" in payload
            else None
        )
        caption = payload.get(caption_key) if caption_key else None
        if caption is not None and (not isinstance(caption, str) or len(caption) > 1000):
            raise ValueError("REVIEW-001 caption_override 長度不合法")
        note = payload.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise ValueError("REVIEW-001 note 長度不合法")
        now = _now()
        with self.database.transaction() as connection:
            photo = connection.execute(
                "SELECT id,lifecycle_status FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
            if photo is None or str(photo["lifecycle_status"]) in {"deleted", "archived"}:
                raise KeyError(photo_id)
            latest = connection.execute(
                "SELECT MAX(id) AS id FROM photo_analysis WHERE photo_id=?", (photo_id,)
            ).fetchone()
            latest_analysis_id = int(latest["id"]) if latest and latest["id"] is not None else None
            decision = connection.execute(
                "SELECT * FROM photo_reviews WHERE photo_id=? AND analysis_id IS NULL ORDER BY id DESC LIMIT 1",
                (photo_id,),
            ).fetchone()
            feedback = (
                connection.execute(
                    "SELECT * FROM photo_reviews WHERE photo_id=? AND analysis_id=? ORDER BY id DESC LIMIT 1",
                    (photo_id, latest_analysis_id),
                ).fetchone()
                if latest_analysis_id is not None
                else None
            )
            if decision is None:
                source = feedback
                # Keep an analysis-scoped compatibility mirror ahead of the
                # durable photo-level row when this is the first review after
                # an analysis was created.  Older readers that select the
                # first row by photo_id still see the review they wrote, while
                # the NULL analysis_id row remains authoritative across
                # re-analysis.
                if latest_analysis_id is not None and source is None:
                    connection.execute(
                        """
                        INSERT INTO photo_reviews(
                            photo_id,analysis_id,review_state,caption_override,candidate_pool,note,
                            accepted_at,version,updated_by,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            photo_id,
                            latest_analysis_id,
                            "unreviewed",
                            None,
                            0,
                            None,
                            None,
                            0,
                            None,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO photo_reviews(
                        photo_id,analysis_id,review_state,caption_override,candidate_pool,note,
                        accepted_at,version,updated_by,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        photo_id,
                        None,
                        str(source["review_state"]) if source is not None else "unreviewed",
                        source["caption_override"] if source is not None else None,
                        int(source["candidate_pool"]) if source is not None else 0,
                        source["note"] if source is not None else None,
                        source["accepted_at"] if source is not None else None,
                        int(source["version"]) if source is not None else 0,
                        source["updated_by"] if source is not None else None,
                        source["updated_at"] if source is not None else now,
                    ),
                )
                current = connection.execute(
                    "SELECT * FROM photo_reviews WHERE photo_id=? AND analysis_id IS NULL ORDER BY id DESC LIMIT 1",
                    (photo_id,),
                ).fetchone()
                decision = current
            assert decision is not None
            logical_current = self._logical_review_row(decision, feedback)
            requested_analysis_id = payload.get("analysis_id")
            if requested_analysis_id is not None and (
                type(requested_analysis_id) is not int or requested_analysis_id < 0
            ):
                raise ValueError("REVIEW-001 analysis_id 必須是非負整數")
            if requested_analysis_id is not None and requested_analysis_id != latest_analysis_id:
                raise ReviewConflictError(self._current_from_row(logical_current))
            if int(logical_current["version"]) != int(expected_version):
                raise ReviewConflictError(self._current_from_row(logical_current))
            before = self._current_from_row(logical_current)
            next_state = state or str(logical_current["review_state"])
            next_caption = caption.strip() if caption_key and caption is not None else (
                None if caption_key else logical_current["caption_override"]
            )
            next_pool = (
                self._bool_payload(payload, "candidate_pool")
                if "candidate_pool" in payload
                else int(logical_current["candidate_pool"])
            )
            if next_state == "exclude":
                if "candidate_pool" in payload and next_pool:
                    raise ValueError("REVIEW-001 exclude 不得同時加入 candidate_pool")
                next_pool = 0
            elif next_state == "needs_review":
                # Pending review is an eligible, explicit selection candidate;
                # it must never retain the permanent-exclude projection.
                if "candidate_pool" in payload and not next_pool:
                    raise ValueError("REVIEW-001 needs_review 必須保留在 candidate_pool")
                next_pool = 1
            elif next_state == "unreviewed":
                next_pool = 0
            next_note = note if "note" in payload else logical_current["note"]
            next_favorite = self._bool_payload(payload, "favorite") if "favorite" in payload else None
            next_understanding = (
                self._bool_payload(payload, "understanding_incorrect")
                if "understanding_incorrect" in payload
                else int(logical_current["understanding_incorrect"] or 0)
            )
            next_caption_bad = (
                self._bool_payload(payload, "caption_bad")
                if "caption_bad" in payload
                else int(logical_current["caption_bad"] or 0)
            )
            next_scores_unreasonable = (
                self._bool_payload(payload, "scores_unreasonable")
                if "scores_unreasonable" in payload
                else int(logical_current["scores_unreasonable"] or 0)
            )
            next_version = int(logical_current["version"]) + 1
            connection.execute(
                """
                UPDATE photo_reviews
                SET review_state=?,caption_override=?,candidate_pool=?,note=?,
                    understanding_incorrect=?,caption_bad=?,scores_unreasonable=?,
                    accepted_at=?,version=?,updated_by=?,updated_at=?
                WHERE id=? AND analysis_id IS NULL AND version=?
                """,
                (
                    next_state,
                    next_caption,
                    next_pool,
                    next_note,
                    next_understanding,
                    next_caption_bad,
                    next_scores_unreasonable,
                    now if next_state == "keep" else None,
                    next_version,
                    actor_id,
                    now,
                    int(decision["id"]),
                    int(logical_current["version"]),
                ),
            )
            if latest_analysis_id is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO photo_reviews(photo_id,analysis_id,updated_at)
                    VALUES (?,?,?)
                    """,
                    (photo_id, latest_analysis_id, now),
                )
                # Keep the latest analysis row as a compatibility mirror for
                # older readers.  The NULL analysis_id row remains the sole
                # durable photo-level decision across re-analysis.
                connection.execute(
                    """
                    UPDATE photo_reviews
                    SET review_state=?,caption_override=?,candidate_pool=?,note=?,
                        understanding_incorrect=?,caption_bad=?,scores_unreasonable=?,
                        accepted_at=?,version=?,updated_by=?,updated_at=?
                    WHERE photo_id=? AND analysis_id=?
                    """,
                    (
                        next_state,
                        next_caption,
                        next_pool,
                        next_note,
                        next_understanding,
                        next_caption_bad,
                        next_scores_unreasonable,
                        now if next_state == "keep" else None,
                        next_version,
                        actor_id,
                        now,
                        photo_id,
                        latest_analysis_id,
                    ),
                )
            if next_state == "keep":
                connection.execute("UPDATE photos SET eligible=1,exclusion_status='manually_restored',reject_reason=NULL,manual_override=1,updated_at=? WHERE id=?", (now, photo_id))
            elif next_state == "exclude":
                connection.execute("UPDATE photos SET eligible=0,exclusion_status='manually_excluded',reject_reason='REVIEW_EXCLUDED',manual_override=1,updated_at=? WHERE id=?", (now, photo_id))
            elif next_state == "needs_review":
                connection.execute("UPDATE photos SET eligible=1,exclusion_status='pending_review',reject_reason='REVIEW_PENDING',manual_override=1,updated_at=? WHERE id=?", (now, photo_id))
            elif next_state == "unreviewed":
                connection.execute("UPDATE photos SET eligible=1,exclusion_status='eligible',reject_reason=NULL,manual_override=0,updated_at=? WHERE id=?", (now, photo_id))
            if next_favorite is not None:
                connection.execute("UPDATE photos SET favorite=?,updated_at=? WHERE id=?", (next_favorite, now, photo_id))
                from inktime.app.repositories.photos import PhotoRepository, invalidate_score_population_cache
                PhotoRepository._refresh_favorite_ranking(connection, photo_id)
                invalidate_score_population_cache()
            current_after = connection.execute(
                "SELECT * FROM photo_reviews WHERE id=?", (int(decision["id"]),)
            ).fetchone()
            if current_after is None:
                raise RuntimeError("REVIEW-500 更新後 Review 資料不存在")
            feedback_after = (
                connection.execute(
                    "SELECT * FROM photo_reviews WHERE photo_id=? AND analysis_id=? ORDER BY id DESC LIMIT 1",
                    (photo_id, latest_analysis_id),
                ).fetchone()
                if latest_analysis_id is not None
                else None
            )
            after = self._current_from_row(self._logical_review_row(current_after, feedback_after))
            connection.execute(
                "INSERT INTO photo_review_events(photo_id,analysis_id,action,before_json,after_json,actor_id,client_version,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    photo_id,
                    latest_analysis_id,
                    "update",
                    _json(before),
                    _json(after),
                    actor_id,
                    expected_version,
                    now,
                ),
            )
        from inktime.app.repositories.photos import invalidate_score_population_cache
        invalidate_score_population_cache()
        return self.get(photo_id) or after

    @staticmethod
    def _logical_review_row(decision: Any, feedback: Any | None) -> dict[str, Any]:
        """Merge durable photo state with latest-analysis feedback."""

        if decision is not None:
            durable = decision
        elif feedback is not None:
            durable = feedback
        else:
            raise RuntimeError("REVIEW-500 缺少 Review projection")
        latest = feedback if feedback is not None else durable
        return {
            "id": durable["id"],
            "photo_id": durable["photo_id"],
            "analysis_id": latest["analysis_id"],
            "review_state": durable["review_state"],
            "caption_override": durable["caption_override"],
            "candidate_pool": durable["candidate_pool"],
            "note": durable["note"],
            "understanding_incorrect": latest["understanding_incorrect"],
            "caption_bad": latest["caption_bad"],
            "scores_unreasonable": latest["scores_unreasonable"],
            "accepted_at": durable["accepted_at"],
            "version": durable["version"],
            "updated_at": durable["updated_at"],
            "updated_by": durable["updated_by"],
        }

    @staticmethod
    def _current_from_row(row: Any) -> dict[str, Any]:
        return {
            "photo_id": str(row["photo_id"]),
            "analysis_id": row["analysis_id"],
            "review_state": str(row["review_state"]),
            "caption_override": row["caption_override"],
            "candidate_pool": bool(row["candidate_pool"]),
            "note": row["note"],
            "understanding_incorrect": bool(row["understanding_incorrect"]),
            "caption_bad": bool(row["caption_bad"]),
            "scores_unreasonable": bool(row["scores_unreasonable"]),
            "accepted_at": row["accepted_at"],
            "version": int(row["version"]),
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    @staticmethod
    def _bool_payload(payload: dict[str, Any], key: str) -> int:
        value = payload.get(key)
        if type(value) is not bool:
            raise ValueError(f"REVIEW-001 {key} 必須是 boolean")
        return int(value)
