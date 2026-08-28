from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Callable, Iterable, Iterator, List
from uuid import uuid4

from inktime.app.db import Database


ACTIVE_STATUSES = {"preparing", "running", "pausing", "retrying"}
TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed", "cancelled"}
MAX_STALE_RECOVERY_ATTEMPTS = 5


class PreviewCapacityError(RuntimeError):
    def __init__(self, scope: str) -> None:
        self.scope = scope
        super().__init__(scope)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def iter_photo_ids(
        self, *, statuses: tuple[str, ...] = ("preprocessed",), limit: int | None = None
    ) -> Iterator[str]:
        placeholders = ",".join("?" for _ in statuses)
        last_id = ""
        remaining = limit
        while remaining is None or remaining > 0:
            batch_size = min(500, remaining) if remaining is not None else 500
            with self.database.session() as connection:
                rows = connection.execute(
                    f"SELECT id FROM photos WHERE lifecycle_status='active' AND status IN ({placeholders}) AND id>? ORDER BY id LIMIT ?",  # noqa: S608 -- placeholders are generated, values remain bound
                    (*statuses, last_id, batch_size),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                yield str(row["id"])
            last_id = str(rows[-1]["id"])
            if remaining is not None:
                remaining -= len(rows)

    def selection_preview(
        self, *, analysis_fingerprint: str | None, selection_mode: str = "pending", limit: int | None = None
    ) -> dict:
        """Bounded SQLite-only pending selector; it never touches image files."""
        fingerprint = str(analysis_fingerprint or "")
        active = "('pending','preparing','running','pausing','retrying')"
        current = "EXISTS (SELECT 1 FROM photo_analysis a WHERE a.photo_id=p.id AND a.analysis_fingerprint=?)"
        queued = f"EXISTS (SELECT 1 FROM job_items ji JOIN jobs j ON j.id=ji.job_id WHERE ji.photo_id=p.id AND ji.status IN ('pending','running','retrying') AND j.status IN {active} AND COALESCE(j.analysis_fingerprint,'')=?)"
        if selection_mode == "force_all":
            predicate = "1=1"
        elif selection_mode == "stale_only":
            predicate = f"NOT {current} AND EXISTS (SELECT 1 FROM photo_analysis a WHERE a.photo_id=p.id)"
        else:
            predicate = f"NOT {current}"
        with self.database.session() as connection:
            # Bind the repeated fingerprint predicates in their SQL order.
            params = [fingerprint, fingerprint]
            if selection_mode != "force_all":
                params.append(fingerprint)
            params.append(fingerprint)
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total_active,
                    SUM(CASE WHEN p.eligible=0 THEN 1 ELSE 0 END) AS excluded,
                    SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM photo_analysis a WHERE a.photo_id=p.id) THEN 1 ELSE 0 END) AS never_analyzed,
                    SUM(CASE WHEN {current} THEN 1 ELSE 0 END) AS already_current,
                    SUM(CASE WHEN {queued} THEN 1 ELSE 0 END) AS already_queued,
                    SUM(CASE WHEN p.eligible=1 AND {predicate} AND NOT ({queued}) THEN 1 ELSE 0 END) AS pending_total,
                    SUM(CASE WHEN NOT ({current}) AND EXISTS (
                        SELECT 1 FROM job_items ji JOIN jobs j ON j.id=ji.job_id
                        WHERE ji.photo_id=p.id AND ji.status='failed'
                          AND COALESCE(j.analysis_fingerprint,'')=?
                    ) THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN EXISTS (SELECT 1 FROM photo_analysis a WHERE a.photo_id=p.id) AND NOT ({current}) THEN 1 ELSE 0 END) AS stale
                FROM photos p WHERE p.lifecycle_status='active'
                """,
                [*params, fingerprint, fingerprint, fingerprint],
            ).fetchone()
            missing = int(
                connection.execute("SELECT COUNT(*) FROM photos WHERE lifecycle_status='missing'").fetchone()[
                    0
                ]
            )
            scan = connection.execute(
                "SELECT completed_at FROM scan_runs WHERE status IN ('completed','completed_with_warnings') "
                "AND completed_at IS NOT NULL ORDER BY completed_at DESC,id DESC LIMIT 1"
            ).fetchone()
            limited_to = (
                min(int(row["pending_total"] or 0), max(0, int(limit)))
                if limit is not None
                else int(row["pending_total"] or 0)
            )
        return {
            **dict(row),
            "missing": missing,
            "pending_total": int(row["pending_total"] or 0),
            "limited_to": limited_to,
            "failed": int(row["failed"] or 0),
            "stale": int(row["stale"] or 0),
            "last_successful_scan_at": str(scan["completed_at"]) if scan else None,
            "selection_mode": selection_mode,
        }

    def iter_pending_photo_ids(
        self, *, analysis_fingerprint: str | None, selection_mode: str = "pending", limit: int | None = None
    ) -> Iterator[str]:
        fingerprint = str(analysis_fingerprint or "")
        active = "('pending','preparing','running','pausing','retrying')"
        current = "EXISTS (SELECT 1 FROM photo_analysis a WHERE a.photo_id=p.id AND a.analysis_fingerprint=?)"
        queued = f"EXISTS (SELECT 1 FROM job_items ji JOIN jobs j ON j.id=ji.job_id WHERE ji.photo_id=p.id AND ji.status IN ('pending','running','retrying') AND j.status IN {active} AND COALESCE(j.analysis_fingerprint,'')=?)"
        predicate = (
            "1=1"
            if selection_mode == "force_all"
            else (
                f"NOT {current} AND EXISTS (SELECT 1 FROM photo_analysis a WHERE a.photo_id=p.id)"
                if selection_mode == "stale_only"
                else f"NOT {current}"
            )
        )
        last_score = float("inf")
        last_id = ""
        remaining = limit
        while remaining is None or remaining > 0:
            size = min(500, remaining) if remaining is not None else 500
            with self.database.session() as connection:
                params = [last_score, last_score, last_id]
                if selection_mode != "force_all":
                    params.append(fingerprint)
                params.extend([fingerprint, size])
                rows = connection.execute(
                    f"SELECT p.id,COALESCE(p.local_candidate_score,-1) AS candidate_score FROM photos p "
                    f"WHERE p.lifecycle_status='active' AND p.eligible=1 AND "
                    f"(COALESCE(p.local_candidate_score,-1) < ? OR (COALESCE(p.local_candidate_score,-1)=? AND p.id>?)) "
                    f"AND {predicate} AND NOT ({queued}) ORDER BY candidate_score DESC,p.id ASC LIMIT ?",
                    params,
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield str(row["id"])
            last_id = str(rows[-1]["id"])
            last_score = float(rows[-1]["candidate_score"])
            if remaining is not None:
                remaining -= len(rows)

    def create(
        self,
        *,
        kind: str = "analysis",
        name: str,
        strategy: str,
        settings: dict,
        photo_ids: Iterable[str],
        created_by: str | None,
        budget_limit: float | None = None,
        priority: int = 3,
        dedupe_key: str | None = None,
        selection_mode: str = "pending",
        analysis_fingerprint: str | None = None,
        force_recompute: bool = False,
        analysis_spec: dict | None = None,
        request_fingerprint: str | None = None,
    ) -> str:
        # Selection can be backed by a generator whose database session is
        # already closed.  Materialize it before acquiring the writer lock so
        # NAS/SQLite iteration cannot run inside the insert transaction.
        photo_ids = [str(photo_id) for photo_id in photo_ids]
        job_id = str(uuid4())
        now = utc_now()
        total = 0
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if dedupe_key:
                    existing = connection.execute(
                        "SELECT id,request_fingerprint FROM jobs WHERE dedupe_key=? ORDER BY created_at DESC,id DESC LIMIT 1",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is not None:
                        stored_fingerprint = str(existing["request_fingerprint"] or "")
                        if stored_fingerprint and request_fingerprint and stored_fingerprint != request_fingerprint:
                            raise ValueError("IDEMPOTENCY_CONFLICT")
                        if request_fingerprint and not stored_fingerprint:
                            connection.execute(
                                "UPDATE jobs SET request_fingerprint=? WHERE id=? AND request_fingerprint IS NULL",
                                (request_fingerprint, existing["id"]),
                            )
                        connection.execute("COMMIT")
                        return str(existing["id"])
                connection.execute(
                    """
                    INSERT INTO jobs(id, kind, name, status, strategy, settings_json,
                                     budget_limit, created_by, created_at, priority, dedupe_key,
                                     selection_mode,analysis_fingerprint,analysis_spec_json,force_recompute,
                                     request_fingerprint)
                    VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?,?,?,?,?)
                    """,
                    (
                        job_id,
                        kind,
                        name,
                        strategy,
                        json.dumps(settings, ensure_ascii=False),
                        budget_limit,
                        created_by,
                        now,
                        max(1, min(int(priority), 6)),
                        dedupe_key,
                        selection_mode,
                        analysis_fingerprint,
                        json.dumps(
                            analysis_spec or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ),
                        int(force_recompute),
                        request_fingerprint,
                    ),
                )
                batch: list[tuple] = []
                for photo_id in photo_ids:
                    batch.append((str(uuid4()), job_id, photo_id, now))
                    if len(batch) == 500:
                        connection.executemany(
                            "INSERT OR IGNORE INTO job_items(id, job_id, photo_id, available_at) VALUES (?, ?, ?, ?)",
                            batch,
                        )
                        total += len(batch)
                        batch.clear()
                if batch:
                    connection.executemany(
                        "INSERT OR IGNORE INTO job_items(id, job_id, photo_id, available_at) VALUES (?, ?, ?, ?)",
                        batch,
                    )
                    total += len(batch)
                # OR IGNORE 可能排除同一工作中的重複照片，以實際筆數為準。
                total = int(
                    connection.execute("SELECT COUNT(*) FROM job_items WHERE job_id=?", (job_id,)).fetchone()[
                        0
                    ]
                )
                connection.execute("UPDATE jobs SET total_items=? WHERE id=?", (total, job_id))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        self.add_event(job_id, "created", f"已建立工作，共 {total} 張照片")
        return job_id

    def get(self, job_id: str):
        with self.database.session() as connection:
            return connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def get_idempotent_request(self, scope_key: str, request_fingerprint: str) -> dict[str, Any] | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM idempotency_requests WHERE scope_key=?", (scope_key,)
            ).fetchone()
        if row is None:
            return None
        if str(row["request_fingerprint"]) != str(request_fingerprint):
            raise ValueError("IDEMPOTENCY_CONFLICT")
        return dict(row)

    def reserve_idempotent_request(
        self,
        scope_key: str,
        request_fingerprint: str,
        request_snapshot: dict[str, Any] | None = None,
        *,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        lease_seconds = max(1, int(lease_seconds))
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        snapshot_json = json.dumps(
            request_snapshot if request_snapshot is not None else {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        reservation_token = str(uuid4())
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM idempotency_requests WHERE scope_key=?", (scope_key,)
                ).fetchone()
                if row is not None:
                    if str(row["request_fingerprint"]) != str(request_fingerprint):
                        raise ValueError("IDEMPOTENCY_CONFLICT")
                    if str(row["status"]) == "completed":
                        connection.execute("COMMIT")
                        result = dict(row)
                        result["reservation_owner"] = False
                        return result
                    connection.execute(
                        """
                        UPDATE idempotency_requests
                        SET reservation_token=?,reservation_expires_at=?,updated_at=?
                        WHERE scope_key=? AND status='in_progress'
                          AND (reservation_token IS NULL OR reservation_expires_at IS NULL OR reservation_expires_at<=?)
                        """,
                        (reservation_token, lease_expires_at, now, scope_key, now),
                    )
                    row = connection.execute(
                        "SELECT * FROM idempotency_requests WHERE scope_key=?", (scope_key,)
                    ).fetchone()
                    connection.execute("COMMIT")
                    result = dict(row)
                    result["reservation_owner"] = str(row["reservation_token"] or "") == reservation_token
                    return result
                connection.execute(
                    """
                    INSERT INTO idempotency_requests(
                        scope_key,request_fingerprint,status,request_snapshot_json,response_json,
                        reservation_token,reservation_expires_at,created_at,updated_at
                    ) VALUES (?,?, 'in_progress',?,NULL,?,?,?,?)
                    """,
                    (
                        scope_key,
                        request_fingerprint,
                        snapshot_json,
                        reservation_token,
                        lease_expires_at,
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "scope_key": scope_key,
                    "request_fingerprint": request_fingerprint,
                    "status": "in_progress",
                    "request_snapshot_json": snapshot_json,
                    "response_json": None,
                    "reservation_token": reservation_token,
                    "reservation_expires_at": lease_expires_at,
                    "reservation_owner": True,
                    "created_at": now,
                    "updated_at": now,
                }
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def freeze_idempotent_request(
        self,
        scope_key: str,
        request_fingerprint: str,
        reservation_token: str,
        request_snapshot: dict[str, Any],
        *,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        """Persist the frozen request snapshot under the reservation owner."""

        lease_seconds = max(1, int(lease_seconds))
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        snapshot_json = json.dumps(request_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM idempotency_requests WHERE scope_key=?", (scope_key,)
                ).fetchone()
                if row is None:
                    raise KeyError(scope_key)
                if str(row["request_fingerprint"]) != str(request_fingerprint):
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                if str(row["status"]) == "completed":
                    connection.execute("COMMIT")
                    result = dict(row)
                    result["reservation_owner"] = False
                    return result
                updated = connection.execute(
                    """
                    UPDATE idempotency_requests
                    SET request_snapshot_json=?,reservation_expires_at=?,updated_at=?
                    WHERE scope_key=? AND status='in_progress' AND reservation_token=?
                      AND reservation_expires_at>?
                    """,
                    (snapshot_json, lease_expires_at, now, scope_key, reservation_token, now),
                ).rowcount
                if updated != 1:
                    raise ValueError("IDEMPOTENCY_RESERVATION_LOST")
                row = connection.execute(
                    "SELECT * FROM idempotency_requests WHERE scope_key=?", (scope_key,)
                ).fetchone()
                connection.execute("COMMIT")
                result = dict(row)
                result["reservation_owner"] = True
                return result
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def renew_idempotent_request(
        self,
        scope_key: str,
        request_fingerprint: str,
        reservation_token: str,
        *,
        lease_seconds: int = 60,
    ) -> bool:
        """Renew a live reservation without changing its durable owner token."""

        lease_seconds = max(1, int(lease_seconds))
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                updated = connection.execute(
                    """
                    UPDATE idempotency_requests
                    SET reservation_expires_at=?,updated_at=?
                    WHERE scope_key=? AND request_fingerprint=? AND status='in_progress'
                      AND reservation_token=? AND reservation_expires_at>?
                    """,
                    (lease_expires_at, now, scope_key, request_fingerprint, reservation_token, now),
                ).rowcount
                connection.execute("COMMIT")
                return updated == 1
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def complete_idempotent_request(
        self,
        scope_key: str,
        request_fingerprint: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        response_json = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM idempotency_requests WHERE scope_key=?", (scope_key,)
                ).fetchone()
                if row is None:
                    raise KeyError(scope_key)
                if str(row["request_fingerprint"]) != str(request_fingerprint):
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                if str(row["status"]) == "in_progress":
                    connection.execute(
                        "UPDATE idempotency_requests SET status='completed',response_json=?,updated_at=? WHERE scope_key=? AND status='in_progress'",
                        (response_json, now, scope_key),
                    )
                row = connection.execute(
                    "SELECT * FROM idempotency_requests WHERE scope_key=?", (scope_key,)
                ).fetchone()
                connection.execute("COMMIT")
                return dict(row)
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def get_by_dedupe_key(self, dedupe_key: str, *, request_fingerprint: str | None = None):
        """Return the durable identity for an explicit idempotency key."""

        if request_fingerprint:
            with self.database.session() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        "SELECT id,status,request_fingerprint FROM jobs WHERE dedupe_key=? ORDER BY created_at DESC,id DESC LIMIT 1",
                        (dedupe_key,),
                    ).fetchone()
                    if row is not None:
                        stored_fingerprint = str(row["request_fingerprint"] or "")
                        if stored_fingerprint and stored_fingerprint != request_fingerprint:
                            raise ValueError("IDEMPOTENCY_CONFLICT")
                        if not stored_fingerprint:
                            connection.execute(
                                "UPDATE jobs SET request_fingerprint=? WHERE id=? AND request_fingerprint IS NULL",
                                (request_fingerprint, row["id"]),
                            )
                            row = connection.execute(
                                "SELECT id,status,request_fingerprint FROM jobs WHERE id=?",
                                (row["id"],),
                            ).fetchone()
                    connection.execute("COMMIT")
                    return row
                except Exception:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT id,status,request_fingerprint FROM jobs WHERE dedupe_key=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (dedupe_key,),
            ).fetchone()
            return row

    def active_dedupe_job(self, dedupe_key: str):
        """Return the newest in-flight Job for a scheduler-owned identity."""

        with self.database.session() as connection:
            return connection.execute(
                """
                SELECT id,status FROM jobs
                WHERE dedupe_key=?
                  AND status IN ('pending','preparing','running','pausing','retrying')
                ORDER BY created_at DESC,id DESC
                LIMIT 1
                """,
                (dedupe_key,),
            ).fetchone()

    def has_active_dedupe(self, dedupe_key: str) -> bool:
        """Return whether a scheduler-owned identity is already in flight."""

        return self.active_dedupe_job(dedupe_key) is not None

    def create_maintenance(
        self,
        *,
        kind: str,
        name: str,
        settings: dict,
        created_by: str | None,
        priority: int | None = None,
        dedupe_key: str | None = None,
        request_fingerprint: str | None = None,
    ) -> str:
        job_id = self._create_maintenance(
            kind=kind,
            name=name,
            settings=settings,
            created_by=created_by,
            priority=priority,
            dedupe_key=dedupe_key,
            request_fingerprint=request_fingerprint,
            transaction_guard=None,
        )
        assert job_id is not None
        return job_id

    def create_maintenance_atomic(
        self,
        *,
        kind: str,
        name: str,
        settings: dict,
        created_by: str | None,
        priority: int | None = None,
        dedupe_key: str | None = None,
        request_fingerprint: str | None = None,
        transaction_guard: Callable[[Any], bool],
    ) -> str | None:
        """Create a maintenance Job under a caller-owned transaction guard."""

        return self._create_maintenance(
            kind=kind,
            name=name,
            settings=settings,
            created_by=created_by,
            priority=priority,
            dedupe_key=dedupe_key,
            request_fingerprint=request_fingerprint,
            transaction_guard=transaction_guard,
        )

    def _create_maintenance(
        self,
        *,
        kind: str,
        name: str,
        settings: dict,
        created_by: str | None,
        priority: int | None = None,
        dedupe_key: str | None = None,
        request_fingerprint: str | None = None,
        transaction_guard: Callable[[Any], bool] | None,
    ) -> str | None:
        if kind not in {
            "scan",
            "backup",
            "render",
            "render_preview",
            "cleanup",
            "virtual_display",
            "webhook",
            "analysis_batch_import",
        }:
            raise ValueError("不支援的維護工作")
        job_id = str(uuid4())
        item_id = str(uuid4())
        now = utc_now()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if dedupe_key:
                    status_clause = (
                        "1=1"
                        if str(dedupe_key).startswith("idempotency:")
                        else (
                        "status NOT IN ('failed','cancelled')"
                        if kind == "backup"
                        else "status IN ('pending','preparing','running','pausing','retrying')"
                        )
                    )
                    existing = connection.execute(
                        f"SELECT id,request_fingerprint FROM jobs WHERE dedupe_key=? AND {status_clause}",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is not None:
                        stored_fingerprint = str(existing["request_fingerprint"] or "")
                        if stored_fingerprint and request_fingerprint and stored_fingerprint != request_fingerprint:
                            raise ValueError("IDEMPOTENCY_CONFLICT")
                        if request_fingerprint and not stored_fingerprint:
                            connection.execute(
                                "UPDATE jobs SET request_fingerprint=? WHERE id=? AND request_fingerprint IS NULL",
                                (request_fingerprint, existing["id"]),
                            )
                        connection.execute("COMMIT")
                        return str(existing["id"])
                if transaction_guard is not None and not transaction_guard(connection):
                    connection.execute("COMMIT")
                    return None
                connection.execute(
                    """
                    INSERT INTO jobs(id,kind,name,status,strategy,settings_json,total_items,created_by,created_at,priority,dedupe_key,request_fingerprint)
                    VALUES (?,?,?,'pending','local',?,1,?,?,?,?,?)
                    """,
                    (
                        job_id,
                        kind,
                        name,
                        json.dumps(settings, ensure_ascii=False),
                        created_by,
                        now,
                        max(1, min(int(priority if priority is not None else self._priority_for(kind)), 6)),
                        dedupe_key,
                        request_fingerprint,
                    ),
                )
                connection.execute(
                    "INSERT INTO job_items(id,job_id,photo_id,available_at) VALUES (?,?,NULL,?)",
                    (item_id, job_id, now),
                )
                connection.execute(
                    "INSERT INTO job_events(job_id,event,message,details_json,created_at) VALUES (?,?,?,?,?)",
                    (
                        job_id,
                        "created",
                        f"已建立 {kind} 維護工作",
                        "{}",
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return job_id

    def create_maintenance_with_capacity(
        self,
        *,
        kind: str,
        name: str,
        settings: dict,
        created_by: str,
        priority: int,
        per_user_limit: int,
        system_limit: int,
    ) -> str:
        """Count and create a one-item Preview Job under one writer lock."""

        if kind != "render_preview":
            raise ValueError("容量限制只適用於 Preview Job")
        job_id = str(uuid4())
        item_id = str(uuid4())
        now = utc_now()
        active = "('pending','preparing','running','pausing','retrying')"
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                user_count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM jobs WHERE kind=? AND created_by=? AND status IN {active}",  # noqa: S608
                        (kind, created_by),
                    ).fetchone()[0]
                )
                if user_count >= max(1, int(per_user_limit)):
                    raise PreviewCapacityError("user")
                system_count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM jobs WHERE kind=? AND status IN {active}",  # noqa: S608
                        (kind,),
                    ).fetchone()[0]
                )
                if system_count >= max(1, int(system_limit)):
                    raise PreviewCapacityError("system")
                connection.execute(
                    """
                    INSERT INTO jobs(
                        id,kind,name,status,strategy,settings_json,total_items,
                        created_by,created_at,priority
                    ) VALUES (?,?,?,'pending','local',?,1,?,?,?)
                    """,
                    (
                        job_id,
                        kind,
                        name,
                        json.dumps(settings, ensure_ascii=False),
                        created_by,
                        now,
                        max(1, min(int(priority), 6)),
                    ),
                )
                connection.execute(
                    "INSERT INTO job_items(id,job_id,photo_id,available_at) VALUES (?,?,NULL,?)",
                    (item_id, job_id, now),
                )
                connection.execute(
                    "INSERT INTO job_events(job_id,event,message,details_json,created_at) VALUES (?,?,?,?,?)",
                    (
                        job_id,
                        "created",
                        f"已建立 {kind} 維護工作",
                        "{}",
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return job_id

    @staticmethod
    def _priority_for(kind: str) -> int:
        return {
            "render": 2,
            "virtual_display": 2,
            "scan": 4,
            "webhook": 5,
            "render_preview": 6,
            "cleanup": 6,
            "backup": 6,
        }.get(kind, 4)

    def active_count(self, kind: str, *, created_by: str | None = None) -> int:
        where = "kind=? AND status IN ('pending','preparing','running','pausing','retrying')"
        parameters: list[object] = [kind]
        if created_by is not None:
            where += " AND created_by=?"
            parameters.append(created_by)
        with self.database.session() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {where}",  # noqa: S608
                parameters,
            ).fetchone()
        return int(row[0]) if row else 0

    def list(self, limit: int = 100):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def list_for_user(self, user_id: str, limit: int = 100):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT * FROM jobs WHERE created_by=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

    def iter_runnable(self, page_size: int = 100):
        """以 keyset 走訪所有可執行 Job，避免舊工作被最新 100 筆遮蔽。"""
        last: tuple[int, str, str] | None = None
        now = utc_now()
        while True:
            where = """
                status IN ('running','retrying')
                AND (
                    EXISTS (
                        SELECT 1 FROM job_items due_items
                        WHERE due_items.job_id=jobs.id
                          AND due_items.status='pending'
                          AND due_items.available_at<=?
                    )
                    OR NOT EXISTS (
                        SELECT 1 FROM job_items active_items
                        WHERE active_items.job_id=jobs.id
                          AND active_items.status IN ('pending','running','retrying')
                    )
                )
            """
            params: list[object] = [now]
            if last is not None:
                where += " AND (priority>? OR (priority=? AND (created_at>? OR (created_at=? AND id>?))))"
                params.extend([last[0], last[0], last[1], last[1], last[2]])
            params.append(max(1, min(page_size, 500)))
            with self.database.session() as connection:
                rows = connection.execute(
                    f"SELECT * FROM jobs WHERE {where} ORDER BY priority ASC,created_at ASC,id ASC LIMIT ?",  # noqa: S608
                    params,
                ).fetchall()
            if not rows:
                return
            yield from rows
            last_row = rows[-1]
            last = (int(last_row["priority"]), str(last_row["created_at"]), str(last_row["id"]))

    def list_items(self, job_id: str, *, limit: int = 100, offset: int = 0):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT * FROM job_items WHERE job_id=? ORDER BY id LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            ).fetchall()

    def failure_codes(self, job_id: str, *, connection=None) -> List[str]:
        context = nullcontext(connection) if connection is not None else self.database.session()
        with context as active_connection:
            rows = active_connection.execute(
                "SELECT error_code FROM job_items WHERE job_id=? AND error_code IS NOT NULL AND error_code<>'' ORDER BY id",
                (job_id,),
            ).fetchall()
        return [str(row["error_code"] or "JOB-003") for row in rows]

    def outcome_codes(self, job_id: str, *, connection=None) -> List[str]:
        """Return structured business outcomes without treating them as errors."""

        context = nullcontext(connection) if connection is not None else self.database.session()
        with context as active_connection:
            rows = active_connection.execute(
                "SELECT result_json FROM job_items WHERE job_id=? AND status='completed' AND result_json IS NOT NULL ORDER BY id",
                (job_id,),
            ).fetchall()
        outcomes: list[str] = []
        for row in rows:
            try:
                result = json.loads(str(row["result_json"]))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(result, dict):
                continue
            code = result.get("outcome_code")
            if code is None and result.get("outcome") == "no_content":
                code = "NO_CONTENT"
            if code:
                outcomes.append(str(code))
        return outcomes

    def can_access(self, job_id: str, user_id: str, *, administrator: bool) -> bool:
        with self.database.session() as connection:
            row = connection.execute("SELECT created_by FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row is not None and (administrator or str(row["created_by"] or "") == str(user_id)))

    def can_access_background_result(self, token: str, user_id: str, *, administrator: bool) -> bool:
        if administrator:
            return True
        marker = f"/background-results/{token}/"
        with self.database.session() as connection:
            rows = connection.execute(
                """
                SELECT i.result_json
                FROM job_items i JOIN jobs j ON j.id=i.job_id
                WHERE j.kind='render_preview' AND j.created_by=?
                  AND i.result_json IS NOT NULL AND i.result_json LIKE ?
                ORDER BY i.completed_at DESC LIMIT 20
                """,
                (user_id, f"%{marker}%"),
            ).fetchall()
        for row in rows:
            try:
                result = json.loads(str(row["result_json"]))
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(result, dict) and any(
                isinstance(value, str) and marker in value for value in result.values()
            ):
                return True
        return False

    def can_commit_item(
        self,
        job_id: str,
        item_id: str,
        worker_id: str,
        idempotency_key: str,
    ) -> bool:
        now = utc_now()
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT j.status job_status,i.status item_status,i.worker_id,
                       i.lease_until,i.idempotency_key
                FROM jobs j JOIN job_items i ON i.job_id=j.id
                WHERE j.id=? AND i.id=?
                """,
                (job_id, item_id),
            ).fetchone()
        return bool(
            row is not None
            and str(row["job_status"]) in {"running", "retrying", "pausing"}
            and str(row["item_status"]) == "running"
            and str(row["worker_id"] or "") == worker_id
            and str(row["idempotency_key"] or "") == idempotency_key
            and str(row["lease_until"] or "") > now
        )

    def add_event(self, job_id: str, event: str, message: str, details: dict | None = None) -> None:
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO job_events(job_id,event,message,details_json,created_at) VALUES (?,?,?,?,?)",
                (job_id, event, message, json.dumps(details or {}, ensure_ascii=False), utc_now()),
            )

    def transition(self, job_id: str, from_statuses: set[str], to_status: str, event: str) -> bool:
        now = utc_now()
        placeholders = ",".join("?" for _ in from_statuses)
        with self.database.session() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET status=?, heartbeat_at=? WHERE id=? AND status IN ({placeholders})",  # noqa: S608 -- only placeholder count is dynamic
                (to_status, now, job_id, *sorted(from_statuses)),
            )
        if cursor.rowcount:
            self.add_event(job_id, event, f"工作狀態已變更為 {to_status}")
        return bool(cursor.rowcount)

    def request_pause(self, job_id: str) -> bool:
        now = utc_now()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                job = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job is None or str(job["status"]) not in {"running", "retrying"}:
                    connection.execute("COMMIT")
                    return False
                active_items = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM job_items WHERE job_id=? AND status='running'",
                        (job_id,),
                    ).fetchone()[0]
                )
                target = "pausing" if active_items else "paused"
                connection.execute(
                    "UPDATE jobs SET status=?,pause_requested_at=?,heartbeat_at=? WHERE id=?",
                    (target, now, now, job_id),
                )
                connection.execute(
                    "INSERT INTO job_events(job_id,event,message,details_json,created_at) VALUES (?,?,?,?,?)",
                    (
                        job_id,
                        "pause_requested" if active_items else "paused",
                        "已要求暫停；目前處理中的項目完成後停止"
                        if active_items
                        else "工作尚無處理中項目，已直接暫停",
                        json.dumps({"active_items": active_items}, ensure_ascii=False),
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def acknowledge_pause(self, job_id: str) -> bool:
        now = utc_now()
        with self.database.session() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status='paused',heartbeat_at=?
                WHERE id=? AND status='pausing'
                  AND NOT EXISTS (
                    SELECT 1 FROM job_items WHERE job_id=jobs.id AND status='running'
                  )
                """,
                (now, job_id),
            )
        if cursor.rowcount:
            self.add_event(job_id, "paused", "工作狀態已變更為 paused")
        return bool(cursor.rowcount)

    def cancel(self, job_id: str) -> bool:
        now = utc_now()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE jobs SET status='cancelled', cancel_requested_at=?, completed_at=?
                    WHERE id=? AND status NOT IN ('completed','completed_with_errors','failed','cancelled')
                    """,
                    (now, now, job_id),
                )
                if cursor.rowcount:
                    connection.execute(
                        "UPDATE job_items SET status='cancelled', completed_at=? WHERE job_id=? AND status IN ('pending','retrying')",
                        (now, job_id),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if cursor.rowcount:
            self.add_event(job_id, "cancelled", "工作已取消，不會再送出新請求")
        return bool(cursor.rowcount)

    def claim(self, job_id: str, worker_id: str, limit: int, lease_seconds: int = 300):
        now = utc_now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                job = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job is None or job["status"] not in {"running", "retrying"}:
                    connection.execute("COMMIT")
                    return []
                rows = connection.execute(
                    """
                SELECT * FROM job_items
                WHERE job_id=? AND status='pending' AND available_at<=?
                ORDER BY available_at ASC,id ASC LIMIT ?
                    """,
                    (job_id, now, limit),
                ).fetchall()
                ids = [row["id"] for row in rows]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    connection.execute(
                        f"""
                        UPDATE job_items SET status='running', worker_id=?, started_at=?,
                                             lease_until=?, attempts=attempts+1,
                                             idempotency_key=COALESCE(idempotency_key,job_id || ':' || id)
                        WHERE id IN ({placeholders})
                        """,
                        (worker_id, now, lease_until, *ids),
                    )
                    rows = connection.execute(
                        f"SELECT * FROM job_items WHERE id IN ({placeholders})",  # noqa: S608 -- ids are bound parameters
                        ids,
                    ).fetchall()
                connection.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now, job_id))
                connection.execute("COMMIT")
                return rows
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def renew_leases(self, job_id: str, worker_id: str, lease_seconds: int = 300) -> int:
        """延長目前 Worker 的租約，避免長時間掃描或模型呼叫被誤判為失聯。"""

        now = utc_now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.session() as connection:
            cursor = connection.execute(
                """
                UPDATE job_items SET lease_until=?
                WHERE job_id=? AND worker_id=? AND status='running'
                """,
                (lease_until, job_id, worker_id),
            )
            connection.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now, job_id))
        return int(cursor.rowcount)

    def complete_item(
        self,
        job_id: str,
        item_id: str,
        result: dict,
        actual_cost: float = 0,
        worker_id: str | None = None,
    ) -> bool:
        now = utc_now()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                status = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                ownership = " AND worker_id=?" if worker_id is not None else ""
                ownership_params = (worker_id,) if worker_id is not None else ()
                if status is None or status["status"] == "cancelled":
                    cursor = connection.execute(
                        f"UPDATE job_items SET status='cancelled', completed_at=?, lease_until=NULL, worker_id=NULL "
                        f"WHERE id=? AND job_id=? AND status='running'{ownership}",  # noqa: S608
                        (now, item_id, job_id, *ownership_params),
                    )
                else:
                    stage = str(result.get("stage") or "completed")[:64]
                    cursor = connection.execute(
                        f"""
                        UPDATE job_items SET status='completed', completed_at=?, result_json=?,
                                             lease_until=NULL, worker_id=NULL, estimated_cost=?, stage=?, error_code=?
                        WHERE id=? AND job_id=? AND status='running'{ownership}
                        """,  # noqa: S608
                        (
                            now,
                            json.dumps(result, ensure_ascii=False),
                            actual_cost,
                            stage,
                            (
                                None
                                if result.get("outcome_code")
                                or result.get("outcome") == "no_content"
                                else str(result.get("error_code") or "") or None
                            ),
                            item_id,
                            job_id,
                            *ownership_params,
                        ),
                    )
                    if cursor.rowcount:
                        connection.execute(
                            "UPDATE jobs SET completed_items=completed_items+1, spent=spent+?, heartbeat_at=? WHERE id=?",
                            (actual_cost, now, job_id),
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return bool(cursor.rowcount)

    def record_late_completion(
        self,
        job_id: str,
        item_id: str,
        result: dict,
        actual_cost: float = 0,
        worker_id: str | None = None,
    ) -> bool:
        """Timeout 後底層 Thread 才結束：只記錄一次診斷，不可轉成正式成功。"""
        now = utc_now()
        with self.database.transaction() as connection:
            ownership = " AND worker_id=?" if worker_id is not None else ""
            ownership_params = (worker_id,) if worker_id is not None else ()
            cursor = connection.execute(
                f"""
                UPDATE job_items
                SET status='failed',completed_at=?,result_json=?,error_code='JOB-004',
                    lease_until=NULL,worker_id=NULL,completion_state='timed_out_completed',dead_lettered_at=?
                WHERE id=? AND job_id=? AND status='running'
                {ownership}
                """,  # noqa: S608
                (now, json.dumps(result, ensure_ascii=False), now, item_id, job_id, *ownership_params),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE jobs SET failed_items=failed_items+1,spent=spent+?,heartbeat_at=?
                    WHERE id=?
                    """,
                    (actual_cost, now, job_id),
                )
        if cursor.rowcount:
            self.add_event(
                job_id,
                "timed_out_completed",
                "工作逾時後才結束；結果僅保留診斷，不會重試或重複套用",
                {"item_id": item_id},
            )
        return bool(cursor.rowcount)

    def defer_item(self, item_id: str, worker_id: str | None = None) -> bool:
        """預算阻擋時歸還租約，不把尚未送出的項目記成分析失敗。"""
        with self.database.session() as connection:
            ownership = " AND worker_id=?" if worker_id is not None else ""
            ownership_params = (worker_id,) if worker_id is not None else ()
            cursor = connection.execute(
                f"""
                UPDATE job_items
                SET status='pending',worker_id=NULL,lease_until=NULL,available_at=?,attempts=MAX(0,attempts-1)
                WHERE id=? AND status='running'{ownership}
                """,  # noqa: S608
                (utc_now(), item_id, *ownership_params),
            )
        return bool(cursor.rowcount)

    def fail_item(
        self,
        job_id: str,
        item_id: str,
        error_code: str,
        message: str,
        *,
        max_attempts: int = 3,
        retry_interval_seconds: int | None = None,
        worker_id: str | None = None,
    ) -> bool:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                item = connection.execute(
                    "SELECT attempts, photo_id FROM job_items WHERE id=? AND job_id=? AND status='running'"
                    + (" AND worker_id=?" if worker_id is not None else ""),
                    (item_id, job_id, *((worker_id,) if worker_id is not None else ())),
                ).fetchone()
                if item is None:
                    connection.execute("COMMIT")
                    return False
                terminal = int(item["attempts"]) >= max_attempts
                if terminal:
                    cursor = connection.execute(
                        """
                        UPDATE job_items SET status='failed', completed_at=?, error_code=?,
                                             lease_until=NULL, worker_id=NULL,
                                             dead_lettered_at=? WHERE id=?
                        """,
                        (now, error_code, now, item_id),
                    )
                    if cursor.rowcount:
                        connection.execute("UPDATE jobs SET failed_items=failed_items+1 WHERE id=?", (job_id,))
                else:
                    delay = (
                        max(1, int(retry_interval_seconds))
                        if retry_interval_seconds is not None
                        else min(300, 2 ** int(item["attempts"]))
                    )
                    available = (now_dt + timedelta(seconds=delay)).isoformat()
                    cursor = connection.execute(
                        "UPDATE job_items SET status='pending', available_at=?, error_code=?, lease_until=NULL, worker_id=NULL WHERE id=?",
                        (available, error_code, item_id),
                    )
                if not cursor.rowcount:
                    connection.execute("COMMIT")
                    return False
                fingerprint = hashlib.sha256(f"{job_id}:{item_id}:{error_code}".encode()).hexdigest()
                existing_error = connection.execute(
                    "SELECT id FROM job_errors WHERE fingerprint=? AND resolved_at IS NULL",
                    (fingerprint,),
                ).fetchone()
                if existing_error:
                    connection.execute(
                        "UPDATE job_errors SET occurrences=occurrences+1,last_seen_at=?,message=? WHERE id=?",
                        (now, message[:1000], existing_error["id"]),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO job_errors(job_id,job_item_id,photo_id,component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at)
                        VALUES (?,?,?,'worker',?,?,?,?,?,?)
                        """,
                        (
                            job_id,
                            item_id,
                            item["photo_id"] if item else None,
                            error_code,
                            fingerprint,
                            "error",
                            message[:1000],
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def finalize_if_done(
        self,
        job_id: str,
        *,
        finalizer: Callable[[Any, str, str], None] | None = None,
    ) -> bool:
        """Finalize a completed Job and any caller-owned durable state atomically."""

        now = utc_now()
        with self.database.transaction() as connection:
            job = connection.execute(
                "SELECT status FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if job is None or str(job["status"]) not in {"running", "retrying"}:
                return False
            counts = connection.execute(
                """
                SELECT SUM(status IN ('pending','running','retrying')) AS active,
                       SUM(status='failed') AS failed
                FROM job_items WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            if counts is None or int(counts["active"] or 0) > 0:
                return False
            target = "completed_with_errors" if int(counts["failed"] or 0) else "completed"
            if finalizer is not None:
                connection.execute("SAVEPOINT job_finalizer")
                try:
                    finalizer(connection, job_id, target)
                except Exception as error:
                    connection.execute("ROLLBACK TO job_finalizer")
                    connection.execute("RELEASE job_finalizer")
                    target = "completed_with_errors"
                    message = str(error)[:1000] or error.__class__.__name__
                    fingerprint = hashlib.sha256(
                        f"{job_id}:finalizer:{error.__class__.__name__}:{message}".encode("utf-8")
                    ).hexdigest()
                    connection.execute(
                        """
                        INSERT INTO job_errors(
                            job_id,job_item_id,photo_id,component,error_code,fingerprint,severity,message,
                            first_seen_at,last_seen_at
                        ) VALUES (?,?,NULL,'finalizer','JOB-FINALIZER-001',?,?,?, ?,?)
                        """,
                        (job_id, None, fingerprint, "error", message, now, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO job_events(job_id,event,message,details_json,created_at)
                        VALUES (?,?,?,?,?)
                        """,
                        (
                            job_id,
                            "finalizer_failed",
                            "工作 finalizer 失敗；已隔離並以 completed_with_errors 結束",
                            json.dumps({"error": message}, ensure_ascii=False),
                            now,
                        ),
                    )
                else:
                    connection.execute("RELEASE job_finalizer")
            cursor = connection.execute(
                "UPDATE jobs SET status=?, completed_at=?, heartbeat_at=? WHERE id=? AND status IN ('running','retrying')",
                (target, now, now, job_id),
            )
        if cursor.rowcount:
            self.add_event(job_id, "finished", f"工作已結束：{target}")
        return bool(cursor.rowcount)

    def recover_stale(self) -> int:
        now = utc_now()
        with self.database.session() as connection:
            candidate = connection.execute(
                """
                SELECT 1 FROM job_items
                WHERE status='running' AND (lease_until IS NULL OR lease_until<?)
                UNION ALL
                SELECT 1 FROM jobs WHERE status='pausing'
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if candidate is None:
                return 0
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT id,job_id,photo_id,attempts FROM job_items
                    WHERE status='running' AND (lease_until IS NULL OR lease_until<?)
                    ORDER BY job_id,id
                    """,
                    (now,),
                ).fetchall()
                recovered = 0
                for row in rows:
                    item_id = str(row["id"])
                    job_id = str(row["job_id"])
                    attempts = int(row["attempts"] or 0)
                    if attempts >= MAX_STALE_RECOVERY_ATTEMPTS:
                        cursor = connection.execute(
                            """
                            UPDATE job_items
                            SET status='failed',completed_at=?,worker_id=NULL,lease_until=NULL,
                                error_code='WORKER_CRASH',dead_lettered_at=?,completion_state='worker_crash'
                            WHERE id=? AND status='running' AND (lease_until IS NULL OR lease_until<?)
                            """,
                            (now, now, item_id, now),
                        )
                        if cursor.rowcount:
                            recovered += 1
                            connection.execute(
                                "UPDATE jobs SET failed_items=failed_items+1,heartbeat_at=? WHERE id=?",
                                (now, job_id),
                            )
                            fingerprint = hashlib.sha256(
                                f"{job_id}:{item_id}:WORKER_CRASH".encode("utf-8")
                            ).hexdigest()
                            connection.execute(
                                """
                                INSERT INTO job_errors(
                                    job_id,job_item_id,photo_id,component,error_code,fingerprint,severity,message,
                                    first_seen_at,last_seen_at
                                ) VALUES (?,?,?,'worker','WORKER_CRASH',?,'error',?,?,?)
                                """,
                                (
                                    job_id,
                                    item_id,
                                    row["photo_id"],
                                    fingerprint,
                                    "Worker lease expired after bounded recovery attempts",
                                    now,
                                    now,
                                ),
                            )
                            connection.execute(
                                """
                                INSERT INTO job_events(job_id,event,message,details_json,created_at)
                                VALUES (?,?,?,?,?)
                                """,
                                (
                                    job_id,
                                    "worker_crash",
                                    "Worker lease 超過上限，工作項目已終止",
                                    json.dumps(
                                        {"item_id": item_id, "attempts": attempts, "error_code": "WORKER_CRASH"},
                                        ensure_ascii=False,
                                    ),
                                    now,
                                ),
                            )
                    else:
                        cursor = connection.execute(
                            """
                            UPDATE job_items
                            SET status='pending',worker_id=NULL,lease_until=NULL,available_at=?
                            WHERE id=? AND status='running' AND (lease_until IS NULL OR lease_until<?)
                            """,
                            (now, item_id, now),
                        )
                        recovered += int(cursor.rowcount)
                connection.execute(
                    """
                    UPDATE jobs SET status='paused',heartbeat_at=?
                    WHERE status='pausing'
                      AND NOT EXISTS (
                        SELECT 1 FROM job_items WHERE job_id=jobs.id AND status='running'
                      )
                    """,
                    (now,),
                )
                connection.execute("COMMIT")
                return recovered
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def retry_failed(self, job_id: str) -> int:
        now = utc_now()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                job = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job is None:
                    connection.execute("COMMIT")
                    return 0
                if str(job["status"]) not in {"failed", "completed_with_errors"}:
                    # A running, paused, cancelled, or already-successful Job
                    # must never be revived by a retry endpoint.
                    connection.execute("COMMIT")
                    return 0
                cursor = connection.execute(
                    """
                    UPDATE job_items SET status='pending', available_at=?, error_code=NULL, completed_at=NULL,
                                         dead_lettered_at=NULL WHERE job_id=? AND status='failed'
                    """,
                    (now, job_id),
                )
                connection.execute(
                    "UPDATE jobs SET status='pending', failed_items=0, completed_at=NULL WHERE id=? AND status IN ('failed','completed_with_errors')",
                    (job_id,),
                )
                connection.execute("COMMIT")
                return int(cursor.rowcount)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def complete_batch_item(
        self, job_id: str, item_id: str, result: dict, actual_cost: float = 0, connection=None
    ) -> bool:
        """Persist a remote Batch result without claiming it in the normal worker queue."""

        now = utc_now()
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            cursor = connection.execute(
                """
                UPDATE job_items SET status='completed',completed_at=?,result_json=?,
                    estimated_cost=?,stage='analysis_batch',lease_until=NULL
                WHERE id=? AND job_id=? AND status NOT IN ('completed','failed','cancelled')
                """,
                (now, json.dumps(result, ensure_ascii=False), actual_cost, item_id, job_id),
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE jobs SET completed_items=completed_items+1,spent=spent+?,heartbeat_at=? WHERE id=?",
                    (actual_cost, now, job_id),
                )
            return bool(cursor.rowcount)

    def fail_batch_item(
        self, job_id: str, item_id: str, error_code: str, message: str, connection=None
    ) -> bool:
        now = utc_now()
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            cursor = connection.execute(
                """
                UPDATE job_items SET status='failed',completed_at=?,error_code=?,stage='analysis_batch',lease_until=NULL
                WHERE id=? AND job_id=? AND status NOT IN ('completed','failed','cancelled')
                """,
                (now, error_code, item_id, job_id),
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE jobs SET failed_items=failed_items+1,heartbeat_at=? WHERE id=?", (now, job_id)
                )
                connection.execute(
                    """
                    INSERT INTO job_errors(job_id,job_item_id,photo_id,component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at)
                    SELECT ?,?,photo_id,'analysis_batch',?,?, 'error',?,?,? FROM job_items WHERE id=?
                    """,
                    (
                        job_id,
                        item_id,
                        error_code,
                        hashlib.sha256(f"{job_id}:{item_id}:{error_code}".encode()).hexdigest(),
                        message[:1000],
                        now,
                        now,
                        item_id,
                    ),
                )
            return bool(cursor.rowcount)

    def cancel_batch_item(
        self, job_id: str, item_id: str, reason: str = "cancelled", connection=None
    ) -> bool:
        """Cancel a Batch item without leaving its parent queue item pending."""

        now = utc_now()
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            cursor = connection.execute(
                """
                UPDATE job_items SET status='cancelled',completed_at=?,error_code=?,stage='analysis_batch',lease_until=NULL
                WHERE id=? AND job_id=? AND status NOT IN ('completed','failed','cancelled')
                """,
                (now, reason[:120], item_id, job_id),
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE jobs SET heartbeat_at=? WHERE id=? AND status!='cancelled'",
                    (now, job_id),
                )
            return bool(cursor.rowcount)

    def finalize_batch_job(self, job_id: str, *, status: str, connection=None) -> bool:
        if status not in {"completed", "completed_with_errors", "failed", "cancelled"}:
            raise ValueError("不合法的 Batch Job terminal status")
        now = utc_now()
        context = self.database.session() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            cursor = connection.execute(
                "UPDATE jobs SET status=?,completed_at=?,heartbeat_at=? WHERE id=? AND status NOT IN ('completed','completed_with_errors','failed','cancelled')",
                (status, now, now, job_id),
            )
        return bool(cursor.rowcount)

    def abandon_unstarted(self, job_id: str, error_code: str = "BATCH-RESERVATION-CONFLICT") -> bool:
        """Close a job whose durable Batch reservation could not be acquired.

        This is intentionally different from worker cancellation: the job has
        never started and must not remain pending/running after a uniqueness
        conflict during Batch item reservation.
        """

        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE jobs SET status='failed',completed_at=?,heartbeat_at=?
                   WHERE id=? AND status='pending'""",
                (now, now, job_id),
            )
            if cursor.rowcount:
                connection.execute(
                    """UPDATE job_items SET status='failed',completed_at=?,error_code=?
                       WHERE job_id=? AND status IN ('pending','retrying')""",
                    (now, error_code, job_id),
                )
        if cursor.rowcount:
            self.add_event(job_id, "reservation_conflict", "Batch reservation 未取得，工作已安全結束")
        return bool(cursor.rowcount)

    def reopen_batch_job(self, job_id: str, connection=None) -> bool:
        """Reopen a Batch parent after manual remote ownership recovery."""

        now = utc_now()
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            cursor = connection.execute(
                """
                UPDATE jobs SET status='running',completed_at=NULL,heartbeat_at=?
                WHERE id=? AND status IN ('pending','running','retrying','failed','completed_with_errors')
                """,
                (now, job_id),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE job_items SET status='pending',completed_at=NULL,error_code=NULL,lease_until=NULL
                    WHERE job_id=? AND status IN ('failed','retrying')
                    """,
                    (job_id,),
                )
                connection.execute(
                    """
                    UPDATE jobs SET completed_items=(
                        SELECT COUNT(*) FROM job_items WHERE job_id=? AND status='completed'
                    ),failed_items=(
                        SELECT COUNT(*) FROM job_items WHERE job_id=? AND status='failed'
                    )
                    WHERE id=?
                    """,
                    (job_id, job_id, job_id),
                )
        if cursor.rowcount and connection is not None and not getattr(connection, "in_transaction", False):
            self.add_event(job_id, "batch_reopened", "人工 Recovery 後重新開啟 Batch 工作")
        return bool(cursor.rowcount)
