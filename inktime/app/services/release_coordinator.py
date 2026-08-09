from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from typing import Any

from inktime.app.db import Database
from inktime.app.domain.rendering import AtomicReleasePublisher
from inktime.app.domain.rendering.release import fsync_directory, release_metadata_guard


class ReleaseCoordinator:
    """協調 Release 檔案、Profile pointer、DB 與顯示歷史的補償式交易。"""

    def __init__(self, database: Database, publisher: AtomicReleasePublisher) -> None:
        self.database = database
        self.publisher = publisher

    def publish(
        self,
        manifests: list[dict[str, Any]],
        *,
        created_by: str,
        photo_ids: list[str],
        history: dict[str, str] | None = None,
        device_assignments: dict[str, str] | None = None,
        activate_pointers: bool = True,
    ) -> list[dict[str, Any]]:
        if not manifests:
            raise ValueError("RENDER-010 沒有可發布的 Release")
        verified = [self.publisher.validate(str(item["release_id"])) for item in manifests]
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.database.transaction() as connection:
                for manifest in verified:
                    connection.execute(
                        """
                        INSERT INTO releases(
                            id,display_type,width,height,pixel_format,manifest_json,status,
                            created_at,created_by,render_profile,verified_at,reconciliation_status
                        ) VALUES (?,?,?,?,?,?,'staged',?,?,?,?, 'ok')
                        """,
                        (
                            manifest["release_id"],
                            manifest["display_type"],
                            manifest["width"],
                            manifest["height"],
                            manifest["pixel_format"],
                            json.dumps(manifest, ensure_ascii=False),
                            manifest["created_at"],
                            created_by,
                            manifest["render_profile"],
                            now,
                        ),
                    )
        except Exception:
            for manifest in verified:
                self.publisher.mark_orphan(str(manifest["release_id"]), "database_stage_failed")
            raise

        snapshot = (
            self.publisher.pointer_snapshot([str(item["render_profile"]) for item in verified])
            if activate_pointers
            else None
        )
        try:
            if activate_pointers and not device_assignments:
                self.publisher.activate_manifests(verified)
            with self.database.transaction() as connection:
                for manifest in verified:
                    connection.execute(
                        "UPDATE releases SET status='published',published_at=?,failure_reason=NULL WHERE id=?",
                        (now, manifest["release_id"]),
                    )
                if device_assignments:
                    connection.executemany(
                        """
                        INSERT INTO device_render_releases(device_id,release_id,assigned_at)
                        VALUES (?,?,?)
                        ON CONFLICT(device_id) DO UPDATE SET
                            release_id=excluded.release_id,assigned_at=excluded.assigned_at
                        """,
                        [
                            (device_id, release_id, now)
                            for device_id, release_id in device_assignments.items()
                        ],
                    )
                if history and photo_ids:
                    history_date = str(history.get("history_date") or now[:10])
                    method = str(history.get("selection_method") or "scheduled")
                    rows: list[tuple[str, str, str, str, str, str]] = []
                    for manifest in verified:
                        rows.extend(
                            (
                                photo_id,
                                history_date,
                                method,
                                manifest["release_id"],
                                now,
                                json.dumps(
                                    {"render_profile": manifest["render_profile"]},
                                    ensure_ascii=False,
                                ),
                            )
                            for photo_id in photo_ids
                        )
                    connection.executemany(
                        """
                        INSERT INTO display_history(
                            photo_id,history_date,selection_method,release_id,displayed_at,metadata_json
                        ) VALUES (?,?,?,?,?,?)
                        """,
                        rows,
                    )
        except Exception as exc:
            if activate_pointers and snapshot is not None:
                self.publisher.restore_pointers(snapshot)
            with self.database.transaction() as connection:
                connection.executemany(
                    "UPDATE releases SET status='staged_failed',failure_reason=? WHERE id=?",
                    [(str(exc)[:500], item["release_id"]) for item in verified],
                )
            raise
        return verified

    def abort_staged(self, release_ids: list[str], reason: str) -> None:
        """Retain failed payloads for reconciliation without leaving them active."""

        identifiers = list(dict.fromkeys(str(value) for value in release_ids if str(value)))
        if not identifiers:
            return
        with self.database.transaction() as connection:
            connection.executemany(
                "UPDATE releases SET status='staged_failed',failure_reason=?,reconciliation_status='aborted' WHERE id=? AND status IN ('staged','published')",
                [(str(reason)[:500], release_id) for release_id in identifiers],
            )
        for release_id in identifiers:
            self.publisher.mark_orphan(release_id, "offline_prepare_aborted")

    def reconcile(self) -> dict[str, int]:
        diagnostics = {
            "staged": 0,
            "payload_missing": 0,
            "orphan": 0,
            "pointer_missing": 0,
            "pointer_recovered": 0,
        }
        with self.database.session() as connection:
            rows = connection.execute("SELECT id,status,render_profile,created_at FROM releases").fetchall()
            known = {str(row["id"]) for row in rows}
        valid: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            release_id = str(row["id"])
            try:
                self.publisher.validate(release_id)
            except ValueError:
                diagnostics["payload_missing"] += 1
                with self.database.session() as connection:
                    connection.execute(
                        "UPDATE releases SET reconciliation_status='payload_missing' WHERE id=?",
                        (release_id,),
                    )
            else:
                if str(row["status"]) == "published":
                    valid.setdefault(str(row["render_profile"]), []).append(
                        (str(row["created_at"]), release_id)
                    )
            if str(row["status"]) == "staged":
                diagnostics["staged"] += 1
        for manifest in self.publisher.list():
            release_id = str(manifest.get("release_id", ""))
            if release_id and release_id not in known:
                diagnostics["orphan"] += 1
                self.publisher.mark_orphan(release_id, "filesystem_release_without_database_row")
        expected_pointers = {"latest", *(f"latest.{profile}" for profile in valid)}
        expected_pointers.update(path.name for path in self.publisher.root.glob("latest*"))
        for pointer_name in sorted(expected_pointers):
            pointer = self.publisher.root / pointer_name
            try:
                release_id = pointer.read_text(encoding="utf-8").strip()
            except OSError:
                release_id = ""
            profile = pointer_name.removeprefix("latest.") if pointer_name != "latest" else ""
            compatible = (
                valid.get(profile, []) if profile else [item for values in valid.values() for item in values]
            )
            valid_ids = {item[1] for item in compatible}
            if release_id not in valid_ids:
                diagnostics["pointer_missing"] += 1
                if compatible:
                    fallback = max(compatible)[1]
                    temporary = self.publisher.root / f".{pointer_name}.reconcile.tmp"
                    temporary.write_text(fallback, encoding="utf-8")
                    with temporary.open("rb") as stream:
                        os.fsync(stream.fileno())
                    temporary.replace(pointer)
                    fsync_directory(self.publisher.root)
                    diagnostics["pointer_recovered"] += 1
        return diagnostics

    def gc_unreferenced_releases(self, *, retention_days: int = 90, max_items: int = 20) -> dict[str, int]:
        """Delete only old superseded payloads with no durable or pointer reference."""

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(retention_days)))).isoformat()
        limit = max(1, min(int(max_items), 100))
        # This snapshot is only a cheap candidate prefilter.  The publisher
        # rechecks authoritative pointers while holding the metadata lock
        # immediately before rename and purge.
        pointer_ids = self.publisher.authoritative_pointer_ids()
        deleted = 0
        skipped = 0
        # A previous maintenance run may have committed the DB delete but
        # failed while purging the quarantine directory.  Reconcile those
        # entries before selecting new candidates; rows still present in the
        # DB are restored instead of being purged.
        for release_id, quarantine in self.publisher.list_gc_quarantines(limit=limit):
            with self.database.session() as connection:
                row = connection.execute("SELECT 1 FROM releases WHERE id=?", (release_id,)).fetchone()
            if row is not None:
                if not self.publisher.restore_quarantined_release(quarantine, release_id):
                    skipped += 1
            elif not self.publisher.purge_gc_quarantine(quarantine):
                skipped += 1

        with self.database.session() as connection:
            candidates = connection.execute(
                """
                SELECT r.id
                FROM releases r
                WHERE r.status IN ('published','superseded','staged','staged_failed')
                  AND r.created_at<?
                  AND NOT EXISTS (SELECT 1 FROM device_render_releases drr WHERE drr.release_id=r.id)
                  AND NOT EXISTS (SELECT 1 FROM device_content_queue_items qi WHERE qi.release_id=r.id)
                  AND NOT EXISTS (SELECT 1 FROM device_offline_schedule_slots os WHERE os.release_id=r.id)
                  AND NOT EXISTS (
                      SELECT 1 FROM device_content_queues q
                      WHERE r.id IN (q.current_release_id,q.last_known_good_release_id,
                                      q.next_queued_release_id,q.emergency_fallback_release_id)
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM rollout_campaigns rc
                      WHERE rc.release_id=r.id OR rc.previous_release_id=r.id
                  )
                  AND NOT EXISTS (SELECT 1 FROM display_history dh WHERE dh.release_id=r.id)
                  AND NOT EXISTS (SELECT 1 FROM selection_decision_traces sdt WHERE sdt.release_id=r.id)
                ORDER BY r.created_at,r.id
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()

        for row in candidates:
            release_id = str(row["id"])
            if release_id in pointer_ids:
                skipped += 1
                continue
            quarantine = None
            committed = False
            # Keep the authoritative pointer fence held across the DB
            # recheck and commit.  All pointer writers use this same metadata
            # guard, so a new active pointer cannot appear after quarantine
            # and before the row is deleted or restored.
            with release_metadata_guard(self.publisher.root):
                try:
                    quarantine = self.publisher.quarantine_release(release_id)
                    if quarantine is None:
                        skipped += 1
                    else:
                        with self.database.session() as connection:
                            connection.execute("BEGIN IMMEDIATE")
                            try:
                                still_unreferenced = connection.execute(
                                    """
                                    SELECT 1
                                    FROM releases r
                                    WHERE r.id=?
                                      AND r.status IN ('published','superseded','staged','staged_failed')
                                      AND NOT EXISTS (SELECT 1 FROM device_render_releases drr WHERE drr.release_id=r.id)
                                      AND NOT EXISTS (SELECT 1 FROM device_content_queue_items qi WHERE qi.release_id=r.id)
                                      AND NOT EXISTS (SELECT 1 FROM device_offline_schedule_slots os WHERE os.release_id=r.id)
                                      AND NOT EXISTS (
                                          SELECT 1 FROM device_content_queues q
                                          WHERE r.id IN (q.current_release_id,q.last_known_good_release_id,
                                                          q.next_queued_release_id,q.emergency_fallback_release_id)
                                      )
                                      AND NOT EXISTS (
                                          SELECT 1 FROM rollout_campaigns rc
                                          WHERE rc.release_id=r.id OR rc.previous_release_id=r.id
                                      )
                                      AND NOT EXISTS (SELECT 1 FROM display_history dh WHERE dh.release_id=r.id)
                                      AND NOT EXISTS (SELECT 1 FROM selection_decision_traces sdt WHERE sdt.release_id=r.id)
                                    """,
                                    (release_id,),
                                ).fetchone()
                                if still_unreferenced is None:
                                    connection.execute("ROLLBACK")
                                else:
                                    cursor = connection.execute(
                                        "DELETE FROM releases WHERE id=? AND status IN ('published','superseded','staged','staged_failed')",
                                        (release_id,),
                                    )
                                    if cursor.rowcount != 1:
                                        connection.execute("ROLLBACK")
                                    else:
                                        connection.execute("COMMIT")
                                        committed = True
                            except Exception:
                                if connection.in_transaction:
                                    connection.execute("ROLLBACK")
                                raise
                        if committed:
                            deleted += 1
                            # A failed purge is intentionally left as an orphan
                            # for the next bounded maintenance pass.  The DB row
                            # is gone, so it cannot be a live release reference.
                            if not self.publisher.purge_gc_quarantine(quarantine):
                                skipped += 1
                    # Restore before releasing the authoritative pointer fence
                    # whenever the DB delete did not commit.
                    if quarantine is not None and not committed and quarantine.exists():
                        if not self.publisher.restore_quarantined_release(quarantine, release_id):
                            raise RuntimeError(f"RENDER-GC quarantine restore failed for {release_id}")
                except Exception as error:
                    # Restore before releasing the authoritative pointer
                    # fence.  A pointer writer must never observe the release
                    # in quarantine while its DB row still exists.
                    if quarantine is not None and not committed and quarantine.exists():
                        if not self.publisher.restore_quarantined_release(quarantine, release_id):
                            raise RuntimeError(f"RENDER-GC quarantine restore failed for {release_id}") from error
                    raise
        return {"deleted": deleted, "skipped": skipped}
