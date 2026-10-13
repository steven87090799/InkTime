from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import tempfile
from pathlib import Path
import time
from typing import Any

from inktime.app.db import Database
from inktime.app.core.logging import log_event
from inktime.app.domain.rendering import AtomicReleasePublisher, DeviceTestReleaseStore
from inktime.app.domain.rendering.release import fsync_directory, release_metadata_guard
from inktime.app.domain.rendering.palette import get_display_profile


LOGGER = logging.getLogger("release")


class ReleaseCoordinator:
    """協調 Release 檔案、Profile pointer、DB 與顯示歷史的補償式交易。"""

    def __init__(self, database: Database, publisher: AtomicReleasePublisher) -> None:
        self.database = database
        self.publisher = publisher

    def _write_rollback_intent(self, payload: dict) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.publisher.root,
                                             prefix=".rollback.", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.publisher.root / ".rollback-intent.json")
            fsync_directory(self.publisher.root)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _clear_rollback_intent(self) -> None:
        (self.publisher.root / ".rollback-intent.json").unlink(missing_ok=True)
        fsync_directory(self.publisher.root)

    def _recover_rollback_locked(self) -> bool:
        path = self.publisher.root / ".rollback-intent.json"
        if not path.exists() and not path.is_symlink():
            return False
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("RENDER-010 回復紀錄不安全，保留待人工核對")
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(intent, dict) or not isinstance(intent.get("profile"), str):
                raise ValueError("invalid rollback intent")
            profile = intent["profile"]
            get_display_profile(profile)
            snapshot = intent["pointers"]
            if (intent["version"] != 1 or not isinstance(snapshot, dict)
                    or set(snapshot) != {"latest", f"latest.{profile}"}
                    or not isinstance(intent["published_at"], str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", intent["release_id"])):
                raise ValueError("invalid rollback intent")
            for value in snapshot.values():
                if value is not None and (not isinstance(value, str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)):
                    raise ValueError("invalid pointer snapshot")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("RENDER-010 回復紀錄無效，保留待人工核對") from exc
        with self.database.session() as connection:
            row = connection.execute("SELECT status,published_at FROM releases WHERE id=?",
                                     (intent["release_id"],)).fetchone()
        if row and row["status"] == "published" and row["published_at"] == intent["published_at"]:
            manifest = self.publisher.validate(intent["release_id"])
            if manifest.get("release_kind", "formal") != "formal" or manifest["render_profile"] != profile:
                raise ValueError("RENDER-010 回復目標不符，保留待人工核對")
            self.publisher.activate_manifests([manifest])
        else:
            self.publisher.restore_pointers(snapshot)
        self._clear_rollback_intent()
        return True

    def rollback(self, release_id: str) -> None:
        with release_metadata_guard(self.publisher.root):
            self._recover_rollback_locked()
            with self.database.session() as connection:
                row = connection.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
            if row is None:
                raise KeyError(release_id)
            if row["status"] not in {"published", "superseded"} or row["reconciliation_status"] == "payload_pruned":
                raise ValueError("RENDER-012 Release 不是可回復的正式版本")
            manifest = self.publisher.validate(release_id)
            profile = str(row["render_profile"])
            if manifest.get("release_kind", "formal") != "formal" or manifest["render_profile"] != profile:
                raise ValueError("RENDER-010 Manifest 與正式版本資料不符")
            snapshot = self.publisher.pointer_snapshot([profile])
            now = datetime.now(timezone.utc).isoformat()
            self._write_rollback_intent({"version": 1, "release_id": release_id, "profile": profile,
                                         "published_at": now, "pointers": snapshot})
            committed = False
            try:
                with self.database.transaction(operation="release_rollback") as connection:
                    connection.execute(
                        "UPDATE releases SET status='superseded' WHERE render_profile=? "
                        "AND status='published' AND id<>?", (profile, release_id),
                    )
                    connection.execute("UPDATE releases SET status='published',published_at=? WHERE id=?",
                                       (now, release_id))
                    self.publisher.activate_manifests([manifest])
                committed = True
                self._clear_rollback_intent()
            except Exception:
                if not committed:
                    self.publisher.restore_pointers(snapshot)
                    self._clear_rollback_intent()
                raise

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
        started = time.monotonic()
        if not manifests:
            raise ValueError("RENDER-010 沒有可發布的 Release")
        log_event(
            LOGGER,
            logging.INFO,
            "Release publish started",
            event="release_publish_started",
            operation="release_publish",
            details={
                "manifest_count": len(manifests),
                "photo_count": len(photo_ids),
                "device_assignment_count": len(device_assignments or {}),
                "activate_pointers": bool(activate_pointers),
            },
        )
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
        except Exception as exc:
            for manifest in verified:
                self.publisher.mark_orphan(str(manifest["release_id"]), "database_stage_failed")
            log_event(
                LOGGER,
                logging.ERROR,
                "Release database staging failed",
                event="release_publish_failed",
                error_code="RENDER-RELEASE-STAGE",
                operation="release_publish",
                duration_ms=int((time.monotonic() - started) * 1000),
                failure_class=type(exc).__name__,
                retryable=False,
                details={"phase": "database_stage", "release_count": len(verified)},
            )
            raise

        with release_metadata_guard(self.publisher.root):
            self._recover_rollback_locked()
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
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "Release activation failed; compensation started",
                    event="release_compensation_started",
                    error_code="RENDER-RELEASE-ACTIVATE",
                    operation="release_publish",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    failure_class=type(exc).__name__,
                    retryable=False,
                    details={"release_count": len(verified)},
                )
                if activate_pointers and snapshot is not None:
                    self.publisher.restore_pointers(snapshot)
                with self.database.transaction() as connection:
                    connection.executemany(
                        "UPDATE releases SET status='staged_failed',failure_reason=? WHERE id=?",
                        [(str(exc)[:500], item["release_id"]) for item in verified],
                    )
                raise
        log_event(
            LOGGER,
            logging.INFO,
            "Release published",
            event="release_published",
            release_id=str(verified[0]["release_id"]) if len(verified) == 1 else "",
            operation="release_publish",
            duration_ms=int((time.monotonic() - started) * 1000),
            details={"release_count": len(verified), "photo_count": len(photo_ids)},
        )
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
        started = time.monotonic()
        with release_metadata_guard(self.publisher.root):
            recovered_rollback = self._recover_rollback_locked()
        log_event(
            LOGGER,
            logging.DEBUG,
            "Release reconciliation started",
            event="release_reconcile_started",
            operation="release_reconcile",
        )
        diagnostics = {
            "staged": 0,
            "payload_missing": 0,
            "orphan": 0,
            "pointer_missing": 0,
            "pointer_recovered": 0,
        }
        if recovered_rollback:
            diagnostics["rollback_recovered"] = 1
        # Snapshot only after any publication's pointer/DB transaction ends.
        # Hash large payloads outside the short metadata lock, then discard
        # stale evidence if publication, rollback or GC changed the snapshot.
        snapshot_sql = "SELECT id,status,render_profile,created_at,reconciliation_status FROM releases ORDER BY id"
        with release_metadata_guard(self.publisher.root), self.database.session() as connection:
            rows = connection.execute(snapshot_sql).fetchall()
        known = {str(row["id"]) for row in rows}
        missing = []
        valid: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            release_id = str(row["id"])
            if str(row["reconciliation_status"]) == "payload_pruned":
                continue
            try:
                self.publisher.validate(release_id)
            except ValueError:
                diagnostics["payload_missing"] += 1
                missing.append(release_id)
            else:
                if str(row["status"]) == "published":
                    valid.setdefault(str(row["render_profile"]), []).append(
                        (str(row["created_at"]), release_id)
                    )
            if str(row["status"]) == "staged":
                diagnostics["staged"] += 1
        with release_metadata_guard(self.publisher.root):
            with self.database.session() as connection:
                current_rows = connection.execute(snapshot_sql).fetchall()
            if [tuple(row) for row in current_rows] != [tuple(row) for row in rows]:
                # A later scheduler tick retries; never overwrite live state
                # using a scan that predates a successful publish or GC.
                diagnostics["deferred"] = 1
            else:
                with self.database.transaction() as connection:
                    connection.executemany(
                        "UPDATE releases SET reconciliation_status='payload_missing' WHERE id=?",
                        [(release_id,) for release_id in missing],
                    )
                self._repair_reconciled_pointers(known, valid, diagnostics)
        actions = sum(int(value) for value in diagnostics.values())
        log_event(
            LOGGER,
            logging.INFO if actions else logging.DEBUG,
            "Release reconciliation completed",
            event="release_reconcile_completed",
            operation="release_reconcile",
            duration_ms=int((time.monotonic() - started) * 1000),
            details=diagnostics,
        )
        return diagnostics

    def _repair_reconciled_pointers(
        self, known: set[str], valid: dict[str, list[tuple[str, str]]], diagnostics: dict[str, int]
    ) -> None:
        """Apply verified snapshot decisions while the metadata guard is held."""
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

    def gc_unreferenced_releases(self, *, retention_days: int = 90, max_items: int = 20) -> dict[str, int]:
        """Prune old payloads while retaining release and display-history metadata."""

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(retention_days)))).isoformat()
        limit = max(1, min(int(max_items), 100))
        # This snapshot is only a cheap candidate prefilter.  The publisher
        # rechecks authoritative pointers while holding the metadata lock
        # immediately before rename and purge.
        pointer_ids = self.publisher.authoritative_pointer_ids()
        deleted = 0
        skipped = 0
        test_assignments = DeviceTestReleaseStore(self.publisher.root)
        # A previous maintenance run may have committed the payload state but
        # failed while purging the quarantine directory.  Reconcile those
        # entries before selecting new candidates; rows not marked pruned are
        # restored instead of being purged.  Missing rows retain compatibility
        # with quarantines created by the former row-deleting GC contract.
        quarantine: Path | None
        for release_id, quarantine in self.publisher.list_gc_quarantines(limit=limit):
            with self.database.session() as connection:
                row = connection.execute(
                    "SELECT reconciliation_status FROM releases WHERE id=?", (release_id,)
                ).fetchone()
            if row is not None and str(row["reconciliation_status"]) != "payload_pruned":
                if not self.publisher.restore_quarantined_release(quarantine, release_id):
                    skipped += 1
            elif not self.publisher.purge_gc_quarantine(quarantine):
                skipped += 1

        with self.database.session() as connection:
            candidates = connection.execute(
                """
                SELECT r.id
                FROM releases r
                WHERE r.status IN ('published','superseded','staged_failed')
                  AND r.reconciliation_status!='payload_pruned'
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
                  AND NOT EXISTS (SELECT 1 FROM selection_decision_traces sdt WHERE sdt.release_id=r.id)
                  AND NOT EXISTS (SELECT 1 FROM rollout_targets rt WHERE rt.rollback_release_id=r.id)
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
            # and before the payload state is committed or restored.
            with release_metadata_guard(self.publisher.root):
                try:
                    with test_assignments.reference_snapshot() as (
                        assignment_ids,
                        assignment_snapshot_complete,
                        _assignment_examined,
                    ):
                        if not assignment_snapshot_complete or release_id in assignment_ids:
                            skipped += 1
                            continue
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
                                      AND r.status IN ('published','superseded','staged_failed')
                                      AND r.reconciliation_status!='payload_pruned'
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
                                      AND NOT EXISTS (SELECT 1 FROM selection_decision_traces sdt WHERE sdt.release_id=r.id)
                                      AND NOT EXISTS (SELECT 1 FROM rollout_targets rt WHERE rt.rollback_release_id=r.id)
                                    """,
                                    (release_id, cutoff),
                                ).fetchone()
                                if still_unreferenced is None:
                                    connection.execute("ROLLBACK")
                                else:
                                    cursor = connection.execute(
                                        """
                                        UPDATE releases
                                        SET reconciliation_status='payload_pruned'
                                        WHERE id=?
                                          AND status IN ('published','superseded','staged_failed')
                                          AND reconciliation_status!='payload_pruned'
                                        """,
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
                            # A failed purge is intentionally left quarantined
                            # for the next bounded maintenance pass.  The DB row
                            # records that the payload was legally pruned.
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
                    LOGGER.warning(
                        "Release payload GC skipped %s after a recoverable failure: %s",
                        release_id,
                        type(error).__name__,
                    )
                    skipped += 1
        return {"deleted": deleted, "skipped": skipped}
