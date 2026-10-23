"""Durable selection snapshots and confirmed panel display history."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime, timezone
import json
from typing import Any
from uuid import uuid4

from inktime.app.domain.rendering.release_sources import release_source_photo_ids


class DisplayHistoryRepository:
    MAX_CANDIDATES = 3000

    def __init__(self, database) -> None:
        self.database = database

    def record_selection(
        self, *, target: date, candidates: list[dict[str, Any]], selected: list[dict[str, Any]],
        method: str, context: dict[str, Any] | None = None,
    ) -> str:
        if len(candidates) > self.MAX_CANDIDATES:
            raise ValueError("選片歷史超出候選池上限")
        run_id = str(uuid4())
        selected_ids = {str(row["id"]) for row in selected}
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO photo_selection_runs
                (id,target_date,method,candidate_count,context_json,created_at) VALUES (?,?,?,?,?,?)""",
                (run_id, target.isoformat(), method, len(candidates), json.dumps(context or {}, ensure_ascii=False), now),
            )
            for rank, row in enumerate(candidates, 1):
                snapshot = {key: row.get(key) for key in (
                    "id", "relative_path", "captured_date", "captured_month_day", "display_count",
                    "last_displayed_at", "selection_count", "last_selected_at", "match_type", "day_distance", "selection_score", "score_kind",
                    "seasonal_priority", "exposure_count", "content_last_exposure_at",
                )}
                connection.execute(
                    """INSERT INTO photo_selection_candidates
                    (run_id,photo_id,rank,selected,score,snapshot_json) VALUES (?,?,?,?,?,?)""",
                    (run_id, row["id"], rank, int(str(row["id"]) in selected_ids),
                     row.get("selection_score", row.get("combined_score")), json.dumps(snapshot, ensure_ascii=False)),
                )
        return run_id

    def selection_priorities(self, run_ids: list[str], photo_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Recover only selected sources' frozen ordering for final pairing."""
        if not run_ids or not photo_ids:
            return {}
        with self.database.session() as connection:
            rows = connection.execute(
                """SELECT requested.value AS photo_id,
                    (SELECT c.snapshot_json FROM photo_selection_candidates c
                     JOIN photo_selection_runs r ON r.id=c.run_id
                     WHERE c.photo_id=requested.value AND c.run_id IN (SELECT value FROM json_each(?))
                     ORDER BY r.created_at DESC,r.id DESC LIMIT 1) AS snapshot
                   FROM json_each(?) requested""",
                (json.dumps(list(dict.fromkeys(run_ids))), json.dumps(list(dict.fromkeys(photo_ids)))),
            ).fetchall()
        priorities = {}
        for row in rows:
            snapshot = json.loads(row["snapshot"] or "{}")
            if snapshot.get("seasonal_priority") is not None:
                priorities[row["photo_id"]] = {key: snapshot[key] for key in (
                    "seasonal_priority", "exposure_count", "content_last_exposure_at",
                ) if snapshot.get(key) is not None}
        return priorities

    def attach_releases(
        self, run_ids: list[str], releases: list[dict[str, Any]], photo_ids: list[str], *, connection=None,
    ) -> None:
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as connection:
            previous_publications: set[str] = set()
            new_publications: set[str] = set()
            for run_id in dict.fromkeys(run_ids):
                run = connection.execute("SELECT * FROM photo_selection_runs WHERE id=?", (run_id,)).fetchone()
                if run is None:
                    raise ValueError("找不到選片歷史")
                candidates = connection.execute(
                    "SELECT photo_id,rank FROM photo_selection_candidates WHERE run_id=?", (run_id,),
                ).fetchall()
                candidate_ids = {row["photo_id"] for row in candidates if row["photo_id"] is not None}
                candidate_count = len(candidates)
                next_rank = max((row["rank"] for row in candidates), default=0)
                # Layout resolution may add a second source after the initial
                # ranking. Always retain and count every actual published source.
                for identifier in set(photo_ids) - candidate_ids:
                    photo = connection.execute("SELECT * FROM photos WHERE id=?", (identifier,)).fetchone()
                    if photo is None:
                        continue
                    next_rank += 1
                    candidate_count += 1
                    connection.execute(
                        """INSERT INTO photo_selection_candidates(run_id,photo_id,rank,selected,snapshot_json)
                        VALUES (?,?,?,1,?)""",
                        (run_id, identifier, next_rank, json.dumps({key: photo[key] for key in (
                            "id", "relative_path", "captured_date", "display_count", "selection_count",
                            "last_displayed_at", "last_selected_at", "captured_month_day",
                        )}, ensure_ascii=False)),
                    )
                    candidate_ids.add(identifier)
                connection.execute("UPDATE photo_selection_runs SET candidate_count=? WHERE id=?", (candidate_count, run_id))
                actual_ids = [identifier for identifier in photo_ids if identifier in candidate_ids]
                for release in releases:
                    options = release.get("render_options") or {}
                    released_ids = release_source_photo_ids(release)
                    if not candidate_ids.intersection(released_ids):
                        continue
                    connection.execute(
                        """INSERT OR IGNORE INTO photo_selection_releases
                        (run_id,release_id,device_id,photo_ids_json) VALUES (?,?,?,?)""",
                        (run_id, release["release_id"], options.get("device_id"), json.dumps(released_ids)),
                    )
                # Pairing can choose a subset of the proposed source pool. Keep
                # the actual published selection separate from proposed flags.
                previous_ids = json.loads(run["published_photo_ids_json"])
                previous_publications.update(previous_ids)
                new_publications.update(actual_ids)
                published_ids = list(dict.fromkeys([*previous_ids, *actual_ids]))
                connection.execute(
                    "UPDATE photo_selection_runs SET published_photo_ids_json=? WHERE id=?",
                    (json.dumps(published_ids), run_id),
                )
            # One publication can evaluate portrait and landscape pools. A
            # source in both pools is still one selection, not two.
            for photo_id in new_publications - previous_publications:
                connection.execute(
                    "UPDATE photos SET selection_count=selection_count+1,last_selected_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), photo_id),
                )

    @staticmethod
    def pagination(page: Any) -> int:
        try:
            return max(1, min(int(page or 1), 100000))
        except (ValueError, TypeError):
            raise ValueError("頁碼必須是整數") from None

    def list(self, *, target_date: str = "", photo_id: str = "", page: Any = 1, tab: str = "selections") -> dict:
        number, size = self.pagination(page), 30
        if target_date:
            date.fromisoformat(target_date)
        clauses, params = ["1=1"], []
        if tab == "displays":
            if target_date:
                clauses.append("h.history_date=?")
                params.append(target_date)
            if photo_id:
                clauses.append("h.photo_id=?")
                params.append(photo_id)
            where = " AND ".join(clauses)
            with self.database.session() as connection:
                total = connection.execute(
                    f"SELECT COUNT(*) FROM display_history h WHERE {where}", params,  # noqa: S608 -- fixed clauses
                ).fetchone()[0]
                rows = connection.execute(
                    f"""SELECT h.*,p.relative_path,p.display_count,d.name device_name FROM display_history h
                    JOIN photos p ON p.id=h.photo_id
                    LEFT JOIN devices d ON d.id=json_extract(h.metadata_json,'$.device_id') WHERE {where}
                    ORDER BY h.displayed_at DESC,h.id DESC LIMIT ? OFFSET ?""",  # noqa: S608 -- fixed clauses
                    [*params, size, (number - 1) * size],
                ).fetchall()
            items = [{**dict(row), "confirmed": row["selection_method"] == "device_queue_ack"} for row in rows]
        else:
            if target_date:
                clauses.append("r.target_date=?")
                params.append(target_date)
            if photo_id:
                clauses.append("EXISTS(SELECT 1 FROM photo_selection_candidates c WHERE c.run_id=r.id AND c.photo_id=?)")
                params.append(photo_id)
            where = " AND ".join(clauses)
            with self.database.session() as connection:
                total = connection.execute(
                    f"SELECT COUNT(*) FROM photo_selection_runs r WHERE {where}", params,  # noqa: S608 -- fixed clauses
                ).fetchone()[0]
                rows = connection.execute(
                    f"""SELECT r.*,(SELECT COUNT(*) FROM photo_selection_releases x WHERE x.run_id=r.id) release_count
                    FROM photo_selection_runs r WHERE {where}
                    ORDER BY r.created_at DESC,r.id DESC LIMIT ? OFFSET ?""",  # noqa: S608 -- fixed clauses
                    [*params, size, (number - 1) * size],
                ).fetchall()
            items = [{**dict(row), "published_photo_ids": json.loads(row["published_photo_ids_json"])} for row in rows]
        return {"items": items, "total": int(total), "page": number, "page_size": size}

    def detail(self, run_id: str, *, page: Any = 1) -> dict | None:
        number, size = self.pagination(page), 60
        with self.database.session() as connection:
            run = connection.execute("SELECT * FROM photo_selection_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                return None
            rows = connection.execute(
                "SELECT * FROM photo_selection_candidates WHERE run_id=? ORDER BY rank LIMIT ? OFFSET ?",
                (run_id, size, (number - 1) * size),
            ).fetchall()
            published_rows = connection.execute(
                """SELECT photo_id,snapshot_json FROM photo_selection_candidates
                WHERE run_id=? AND json_extract(snapshot_json,'$.id') IN (SELECT value FROM json_each(?))
                ORDER BY rank""", (run_id, run["published_photo_ids_json"]),
            ).fetchall()
            releases = connection.execute(
                """SELECT x.*,r.status,d.name device_name,
                (SELECT COUNT(*) FROM display_history h WHERE h.release_id=x.release_id
                 AND h.selection_method='device_queue_ack'
                 AND (x.device_id IS NULL OR json_extract(h.metadata_json,'$.device_id')=x.device_id)) confirmed_count
                FROM photo_selection_releases x LEFT JOIN releases r ON r.id=x.release_id
                LEFT JOIN devices d ON d.id=x.device_id WHERE x.run_id=?""", (run_id,),
            ).fetchall()
        published = json.loads(run["published_photo_ids_json"])
        return {
            **dict(run), "context": json.loads(run["context_json"]), "published_photo_ids": published,
            "published_photos": [{"photo_id": row["photo_id"], **json.loads(row["snapshot_json"])} for row in published_rows],
            "candidates": [{**dict(row), "snapshot": json.loads(row["snapshot_json"]),
                            "published": json.loads(row["snapshot_json"]).get("id") in published} for row in rows],
            "releases": [dict(row) for row in releases], "page": number, "page_size": size,
        }
