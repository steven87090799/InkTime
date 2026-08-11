from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import time
from typing import Any

from inktime.app.db import Database
from inktime.app.domain.rendering import AtomicReleasePublisher
from inktime.app.core.logging import log_event


LOGGER = logging.getLogger("release")


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
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        if not manifests:
            raise ValueError("RENDER-010 沒有可發布的 Release")
        release_id = str(manifests[0].get("release_id") or "")
        log_event(
            LOGGER,
            logging.INFO,
            "Release publish started",
            event="release_publish_started",
            release_id=release_id,
            phase="validation",
            details={
                "release_count": len(manifests),
                "photo_count": len(photo_ids),
                "device_count": len(device_assignments or {}),
            },
        )
        verified = [self.publisher.validate(str(item["release_id"])) for item in manifests]
        log_event(
            LOGGER,
            logging.DEBUG,
            "Release validation completed",
            event="release_validation_completed",
            release_id=release_id,
            phase="validation",
            details={
                "release_count": len(verified),
                "render_profiles": [str(item["render_profile"]) for item in verified],
                "payload_count": sum(len(item.get("files") or []) for item in verified),
            },
        )
        now = datetime.now(timezone.utc).isoformat()
        try:
            log_event(
                LOGGER,
                logging.DEBUG,
                "Release database staging started",
                event="release_db_stage_started",
                release_id=release_id,
                phase="db_stage",
            )
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
            log_event(
                LOGGER,
                logging.DEBUG,
                "Release database staging completed",
                event="release_db_staged",
                release_id=release_id,
                phase="db_stage",
            )
        except Exception as exc:
            for manifest in verified:
                self.publisher.mark_orphan(str(manifest["release_id"]), "database_stage_failed")
            log_event(
                LOGGER,
                logging.ERROR,
                "Release database staging failed",
                event="release_publish_failed",
                error_code="RELEASE-DB-001",
                release_id=release_id,
                phase="db_stage",
                duration_ms=int((time.monotonic() - started) * 1000),
                failure_class=type(exc).__name__,
                retryable=True,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise

        snapshot = self.publisher.pointer_snapshot(
            [str(item["render_profile"]) for item in verified]
        )
        log_event(
            LOGGER,
            logging.DEBUG,
            "Release pointer snapshot created",
            event="release_pointer_snapshot_created",
            release_id=release_id,
            phase="pointer_snapshot",
            details={"pointer_count": len(snapshot)},
        )
        try:
            if not device_assignments:
                log_event(
                    LOGGER,
                    logging.DEBUG,
                    "Release pointer activation started",
                    event="release_pointer_activation_started",
                    release_id=release_id,
                    phase="pointer_activation",
                )
                self.publisher.activate_manifests(verified)
                log_event(
                    LOGGER,
                    logging.DEBUG,
                    "Release pointers activated",
                    event="release_pointer_activated",
                    release_id=release_id,
                    phase="pointer_activation",
                )
            log_event(
                LOGGER,
                logging.DEBUG,
                "Release database publish started",
                event="release_db_publish_started",
                release_id=release_id,
                phase="db_publish",
            )
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
                        [(device_id, release_id, now) for device_id, release_id in device_assignments.items()],
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
                "Release publish failed; compensation started",
                event="release_publish_failed",
                error_code="RELEASE-PUBLISH-001",
                release_id=release_id,
                phase="publish",
                duration_ms=int((time.monotonic() - started) * 1000),
                failure_class=type(exc).__name__,
                retryable=False,
                ambiguous=True,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            log_event(
                LOGGER,
                logging.WARNING,
                "Release compensation started",
                event="release_compensation_started",
                release_id=release_id,
                phase="compensation",
            )
            if not device_assignments:
                try:
                    self.publisher.restore_pointers(snapshot)
                except Exception as compensation_exc:
                    log_event(
                        LOGGER,
                        logging.CRITICAL,
                        "Release pointer compensation failed",
                        event="release_compensation_failed",
                        error_code="RELEASE-COMPENSATE-001",
                        release_id=release_id,
                        phase="compensation",
                        failure_class=type(compensation_exc).__name__,
                        retryable=False,
                        ambiguous=True,
                        exc_info=(
                            type(compensation_exc),
                            compensation_exc,
                            compensation_exc.__traceback__,
                        ),
                    )
                    raise
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "Release pointers restored",
                    event="release_pointer_restored",
                    release_id=release_id,
                    phase="compensation",
                )
            with self.database.transaction() as connection:
                connection.executemany(
                    "UPDATE releases SET status='staged_failed',failure_reason=? WHERE id=?",
                    [(str(exc)[:500], item["release_id"]) for item in verified],
                )
            log_event(
                LOGGER,
                logging.WARNING,
                "Release compensation completed",
                event="release_compensation_completed",
                release_id=release_id,
                phase="compensation",
            )
            raise
        log_event(
            LOGGER,
            logging.INFO,
            "Release published",
            event="release_published",
            release_id=release_id,
            phase="completed",
            duration_ms=int((time.monotonic() - started) * 1000),
            details={
                "release_count": len(verified),
                "photo_count": len(photo_ids),
                "device_count": len(device_assignments or {}),
            },
        )
        return verified

    def reconcile(self) -> dict[str, int]:
        started = time.monotonic()
        log_event(
            LOGGER,
            logging.DEBUG,
            "Release reconciliation started",
            event="release_reconcile_started",
            phase="reconciliation",
        )
        diagnostics = {
            "staged": 0,
            "payload_missing": 0,
            "orphan": 0,
            "pointer_missing": 0,
            "pointer_recovered": 0,
        }
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT id,status,render_profile,created_at FROM releases"
            ).fetchall()
            known = {str(row["id"]) for row in rows}
        valid: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            release_id = str(row["id"])
            try:
                self.publisher.validate(release_id)
            except ValueError:
                diagnostics["payload_missing"] += 1
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "Published release payload is missing or invalid",
                    event="release_payload_missing",
                    error_code="RENDER-010",
                    release_id=release_id,
                    phase="reconciliation",
                )
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
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "Orphan filesystem release detected",
                    event="release_orphan_detected",
                    release_id=release_id,
                    phase="reconciliation",
                )
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
                valid.get(profile, [])
                if profile
                else [item for values in valid.values() for item in values]
            )
            valid_ids = {item[1] for item in compatible}
            if release_id not in valid_ids:
                diagnostics["pointer_missing"] += 1
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "Release pointer is missing or invalid",
                    event="release_pointer_invalid",
                    phase="reconciliation",
                    details={"pointer_category": pointer_name},
                )
                if compatible:
                    fallback = max(compatible)[1]
                    temporary = self.publisher.root / f".{pointer_name}.reconcile.tmp"
                    temporary.write_text(fallback, encoding="utf-8")
                    temporary.replace(pointer)
                    diagnostics["pointer_recovered"] += 1
                    log_event(
                        LOGGER,
                        logging.WARNING,
                        "Release pointer recovered",
                        event="release_pointer_recovered",
                        release_id=fallback,
                        phase="reconciliation",
                        details={"pointer_category": pointer_name},
                    )
        actions = sum(diagnostics.values())
        log_event(
            LOGGER,
            logging.INFO if actions else logging.DEBUG,
            "Release reconciliation completed",
            event="release_reconcile_completed",
            phase="reconciliation",
            duration_ms=int((time.monotonic() - started) * 1000),
            details=diagnostics,
        )
        return diagnostics
