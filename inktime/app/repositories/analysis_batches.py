"""Durable SQLite state for the asynchronous photo-analysis Batch lifecycle."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import json
from typing import Any, Iterable, List

from inktime.app.db import Database


ACTIVE_BATCH_STATUSES = {
    "preparing",
    "uploading",
    "upload_unknown",
    "uploaded",
    "submitting",
    "submission_unknown",
    "validating",
    "in_progress",
    "finalizing",
    "import_pending",
    "importing",
    "cancelling",
    "cleanup_pending",
}
TERMINAL_BATCH_STATUSES = {"completed", "completed_with_errors", "failed", "expired", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AnalysisBatchRepository:
    """Small, explicit repository; remote state never lives only in settings JSON."""

    BATCH_FIELDS = {
        "job_id",
        "provider_id",
        "model",
        "endpoint",
        "analysis_fingerprint",
        "status",
        "upload_attempt_id",
        "submission_attempt_id",
        "side_effect_version",
        "side_effect_lease_until",
        "side_effect_owner",
        "phase_started_at",
        "abandon_confirmed_at",
        "input_file_id",
        "input_file_bytes",
        "remote_batch_id",
        "output_file_id",
        "error_file_id",
        "input_file_deleted",
        "output_file_deleted",
        "error_file_deleted",
        "local_input_path",
        "local_output_path",
        "local_error_path",
        "total_items",
        "completed_items",
        "failed_items",
        "missing_items",
        "stale_items",
        "imported_items",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "reasoning_tokens",
        "estimated_cost",
        "actual_cost",
        "last_error_code",
        "last_error_message",
        "submitted_at",
        "last_polled_at",
        "completed_at",
        "cleanup_completed_at",
        "remote_status",
        "sample_seed",
        "candidate_snapshot_json",
        "scope",
        "peak_rss_bytes",
        "cleanup_status",
    }
    ITEM_FIELDS = {
        "batch_id",
        "job_item_id",
        "photo_id",
        "custom_id",
        "content_sha256",
        "analysis_fingerprint",
        "vision_request_fingerprint",
        "vision_input_spec_json",
        "status",
        "request_id",
        "http_status",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "reasoning_tokens",
        "estimated_cost",
        "actual_cost",
        "raw_response_json",
        "error_code",
        "error_message",
        "imported_at",
    }

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_with_items(self, batch: dict[str, Any], items: Iterable[dict[str, Any]]) -> int:
        now = utc_now()
        item_rows = list(items)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO analysis_batches(
                    id,job_id,provider_id,model,endpoint,analysis_fingerprint,status,
                    total_items,estimated_cost,sample_seed,candidate_snapshot_json,scope,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(batch["id"]),
                    batch.get("job_id"),
                    batch.get("provider_id"),
                    str(batch["model"]),
                    str(batch["endpoint"]),
                    str(batch["analysis_fingerprint"]),
                    str(batch.get("status", "preparing")),
                    len(item_rows),
                    float(batch.get("estimated_cost", 0) or 0),
                    batch.get("sample_seed"),
                    str(batch.get("candidate_snapshot_json", "[]")),
                    str(batch.get("scope", "all_eligible_missing_analysis")),
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO analysis_batch_items(
                    id,batch_id,job_item_id,photo_id,custom_id,content_sha256,
                    analysis_fingerprint,vision_request_fingerprint,vision_input_spec_json,
                    status,estimated_cost,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        str(item["id"]),
                        str(batch["id"]),
                        item.get("job_item_id"),
                        item.get("photo_id"),
                        str(item["custom_id"]),
                        str(item["content_sha256"]),
                        str(item["analysis_fingerprint"]),
                        str(item["vision_request_fingerprint"]),
                        str(item["vision_input_spec_json"]),
                        str(item.get("status", "pending")),
                        float(item.get("estimated_cost", 0) or 0),
                        now,
                        now,
                    )
                    for item in item_rows
                ],
            )
        return len(item_rows)

    def create_child_batch(
        self,
        parent_id: str,
        child_id: str,
        item_ids: Iterable[str],
        *,
        local_input_path: str,
        total_items: int,
        peak_rss_bytes: int = 0,
        input_file_bytes: int | None = None,
    ) -> int:
        """Split a locally prepared shard without losing the durable item mapping."""

        selected = [str(value) for value in item_ids]
        if not selected:
            raise ValueError("Batch child 不可沒有項目")
        now = utc_now()
        with self.database.transaction() as connection:
            parent = connection.execute("SELECT * FROM analysis_batches WHERE id=?", (parent_id,)).fetchone()
            if parent is None:
                raise KeyError(parent_id)
            placeholders = ",".join("?" for _ in selected)
            rows = connection.execute(
                f"SELECT id FROM analysis_batch_items WHERE batch_id=? AND id IN ({placeholders})",  # noqa: S608
                (parent_id, *selected),
            ).fetchall()
            if len(rows) != len(selected):
                raise ValueError("Batch child 含有不屬於父 Batch 的項目")
            connection.execute(
                """
                INSERT INTO analysis_batches(
                    id,job_id,provider_id,model,endpoint,analysis_fingerprint,status,
                    local_input_path,total_items,estimated_cost,sample_seed,candidate_snapshot_json,scope,
                    peak_rss_bytes,input_file_bytes,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    child_id,
                    parent["job_id"],
                    parent["provider_id"],
                    parent["model"],
                    parent["endpoint"],
                    parent["analysis_fingerprint"],
                    "preparing",
                    local_input_path,
                    int(total_items),
                    float(parent["estimated_cost"] or 0)
                    * int(total_items)
                    / max(1, int(parent["total_items"] or 1)),
                    parent["sample_seed"],
                    parent["candidate_snapshot_json"],
                    parent["scope"],
                    int(peak_rss_bytes),
                    input_file_bytes,
                    now,
                    now,
                ),
            )
            connection.execute(
                f"UPDATE analysis_batch_items SET batch_id=?,updated_at=? WHERE id IN ({placeholders})",  # noqa: S608
                (child_id, now, *selected),
            )
            connection.execute(
                "UPDATE analysis_batches SET total_items=?,updated_at=? WHERE id=?",
                (int(parent["total_items"] or 0) - len(selected), now, parent_id),
            )
        return len(selected)

    def get(self, batch_id: str):
        with self.database.session() as connection:
            return connection.execute("SELECT * FROM analysis_batches WHERE id=?", (batch_id,)).fetchone()

    def list(self, *, statuses: set[str] | None = None, limit: int = 100) -> List[dict]:
        params: list[Any] = []
        where = ""
        if statuses:
            values = sorted(statuses)
            where = f"WHERE status IN ({','.join('?' for _ in values)})"
            params.extend(values)
        params.append(max(1, min(int(limit), 500)))
        with self.database.session() as connection:
            rows = connection.execute(
                f"SELECT * FROM analysis_batches {where} ORDER BY created_at DESC,id DESC LIMIT ?",  # noqa: S608
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_pollable_due(self, *, limit: int = 20) -> List[dict]:
        """Return only scheduler-owned work, oldest due record first.

        Unknown external side effects are deliberately excluded.  They are an
        operator queue, not scheduler work, so a large number of holds cannot
        consume the bounded poll budget for live or cleanup batches.
        """

        statuses = sorted(ACTIVE_BATCH_STATUSES - {"upload_unknown", "submission_unknown"})
        placeholders = ",".join("?" for _ in statuses)
        now = utc_now()
        with self.database.session() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM analysis_batches
                WHERE status IN ({placeholders})
                  AND (side_effect_lease_until IS NULL OR side_effect_lease_until<=?)
                ORDER BY COALESCE(last_polled_at,phase_started_at,updated_at,created_at) ASC,id ASC
                LIMIT ?
                """,  # noqa: S608 -- only the allowlisted status count is dynamic.
                (*statuses, now, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_operator_holds(self, *, limit: int = 100) -> List[dict]:
        """List ambiguous side effects without mixing them into poll work."""

        return self.list(statuses={"upload_unknown", "submission_unknown"}, limit=limit)

    def list_for_job(self, job_id: str, *, limit: int = 500) -> List[dict]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT * FROM analysis_batches WHERE job_id=? ORDER BY created_at ASC,id ASC LIMIT ?",
                (job_id, max(1, min(int(limit), 5000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def items(self, batch_id: str, *, statuses: set[str] | None = None) -> List[dict]:
        params: list[Any] = [batch_id]
        where = "batch_id=?"
        if statuses:
            values = sorted(statuses)
            where += f" AND status IN ({','.join('?' for _ in values)})"
            params.extend(values)
        with self.database.session() as connection:
            rows = connection.execute(
                f"SELECT * FROM analysis_batch_items WHERE {where} ORDER BY id",  # noqa: S608
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def item_by_custom_id(self, custom_id: str) -> dict | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_batch_items WHERE custom_id=?", (custom_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def update_batch(self, batch_id: str, *, connection=None, **changes: Any) -> None:
        unknown = set(changes) - self.BATCH_FIELDS
        if unknown:
            raise ValueError(f"不支援的 Batch 欄位: {', '.join(sorted(unknown))}")
        if not changes:
            return
        changes = dict(changes)
        changes["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in changes)
        values = [changes[key] for key in changes]
        values.append(batch_id)
        context = self.database.session() if connection is None else nullcontext(connection)
        with context as active_connection:
            cursor = active_connection.execute(
                f"UPDATE analysis_batches SET {assignments} WHERE id=?",  # noqa: S608
                values,
            )
        if cursor.rowcount != 1:
            raise KeyError(batch_id)

    def update_item(self, item_id: str, *, connection=None, **changes: Any) -> None:
        unknown = set(changes) - self.ITEM_FIELDS
        if unknown:
            raise ValueError(f"不支援的 Batch Item 欄位: {', '.join(sorted(unknown))}")
        if not changes:
            return
        changes = dict(changes)
        changes["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in changes)
        values = [changes[key] for key in changes]
        values.append(item_id)
        context = self.database.session() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            cursor = connection.execute(
                f"UPDATE analysis_batch_items SET {assignments} WHERE id=?",  # noqa: S608
                values,
            )
        if cursor.rowcount != 1:
            raise KeyError(item_id)

    def claim_poll(self, batch_id: str, owner: str, lease_until: str, expected_version: int) -> bool:
        """Claim one scheduler poll without allowing two schedulers to poll it."""

        statuses = sorted(ACTIVE_BATCH_STATUSES - {"upload_unknown", "submission_unknown"})
        placeholders = ",".join("?" for _ in statuses)
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE analysis_batches
                SET side_effect_owner=?,side_effect_lease_until=?,side_effect_version=side_effect_version+1,
                    last_polled_at=?,updated_at=?
                WHERE id=? AND status IN ({placeholders}) AND side_effect_version=?
                  AND (side_effect_lease_until IS NULL OR side_effect_lease_until<=?)
                """,  # noqa: S608 -- only the allowlisted status count is dynamic.
                (owner, lease_until, now, now, batch_id, *statuses, int(expected_version), now),
            )
        return bool(cursor.rowcount)

    def release_side_effect_claim(self, batch_id: str, owner: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_batches
                SET side_effect_owner=NULL,side_effect_lease_until=NULL,
                    side_effect_version=side_effect_version+1,updated_at=?
                WHERE id=? AND side_effect_owner=?
                """,
                (utc_now(), batch_id, owner),
            )
        return bool(cursor.rowcount)

    def claim_upload(self, batch_id: str, owner: str, attempt_id: str, lease_until: str) -> bool:
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_batches
                SET status='uploading',upload_attempt_id=?,side_effect_owner=?,side_effect_lease_until=?,
                    side_effect_version=side_effect_version+1,phase_started_at=?,updated_at=?,
                    last_error_code=NULL,last_error_message=NULL
                WHERE id=? AND status='preparing' AND input_file_id IS NULL
                  AND (side_effect_lease_until IS NULL OR side_effect_lease_until<=?)
                """,
                (attempt_id, owner, lease_until, now, now, batch_id, now),
            )
        return bool(cursor.rowcount)

    def complete_upload(
        self,
        batch_id: str,
        attempt_id: str,
        owner: str,
        input_file_id: str,
        input_file_bytes: int | None = None,
    ) -> bool:
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_batches
                SET input_file_id=?,input_file_bytes=?,status='uploaded',phase_started_at=?,updated_at=?,
                    side_effect_owner=NULL,side_effect_lease_until=NULL,side_effect_version=side_effect_version+1
                WHERE id=? AND status='uploading' AND upload_attempt_id=? AND side_effect_owner=?
                """,
                (input_file_id, input_file_bytes, now, now, batch_id, attempt_id, owner),
            )
        return bool(cursor.rowcount)

    def mark_upload_unknown(
        self, batch_id: str, attempt_id: str, owner: str, error_code: str, error_message: str
    ) -> bool:
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_batches
                SET status='upload_unknown',remote_status='upload_unknown',cleanup_status='pending',
                    last_error_code=?,last_error_message=?,completed_at=NULL,phase_started_at=?,updated_at=?,
                    side_effect_owner=NULL,side_effect_lease_until=NULL,side_effect_version=side_effect_version+1
                WHERE id=? AND status='uploading' AND upload_attempt_id=? AND side_effect_owner=?
                """,
                (error_code, error_message[:1000], now, now, batch_id, attempt_id, owner),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE analysis_batch_items SET status='upload_unknown',error_code=?,error_message=?,updated_at=?
                    WHERE batch_id=? AND status='pending'
                    """,
                    (error_code, error_message[:1000], now, batch_id),
                )
        return bool(cursor.rowcount)

    def claim_submission(self, batch_id: str, owner: str, attempt_id: str, lease_until: str) -> bool:
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_batches
                SET status='submitting',submission_attempt_id=?,side_effect_owner=?,side_effect_lease_until=?,
                    side_effect_version=side_effect_version+1,phase_started_at=?,submitted_at=COALESCE(submitted_at,?),
                    updated_at=?,last_error_code=NULL,last_error_message=NULL
                WHERE id=? AND status='uploaded' AND input_file_id IS NOT NULL
                  AND (side_effect_lease_until IS NULL OR side_effect_lease_until<=?)
                """,
                (attempt_id, owner, lease_until, now, now, now, batch_id, now),
            )
        return bool(cursor.rowcount)

    def claim_cancel(self, batch_id: str, owner: str, lease_until: str) -> bool:
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_batches
                SET status='cancelling',side_effect_owner=?,side_effect_lease_until=?,
                    side_effect_version=side_effect_version+1,phase_started_at=?,updated_at=?
                WHERE id=? AND remote_batch_id IS NOT NULL
                  AND status IN ('validating','in_progress','finalizing','cancelling')
                  AND (side_effect_lease_until IS NULL OR side_effect_lease_until<=?)
                """,
                (owner, lease_until, now, now, batch_id, now),
            )
        return bool(cursor.rowcount)

    def complete_submission(
        self,
        batch_id: str,
        attempt_id: str,
        owner: str,
        remote: dict[str, Any],
    ) -> str | None:
        remote_id = str(remote.get("id") or remote.get("remote_batch_id") or "")
        remote_status = str(remote.get("status") or "validating")
        if not remote_id:
            raise ValueError("BATCH-SUBMISSION-UNKNOWN 遠端 Batch 缺少 id")
        if remote_status not in {
            "validating",
            "in_progress",
            "finalizing",
            "completed",
            "failed",
            "expired",
            "cancelling",
            "cancelled",
        }:
            raise ValueError("BATCH-REMOTE-001 遠端 Batch 缺少有效 status")
        terminal = remote_status in {"completed", "failed", "expired", "cancelled"}
        local_status = "import_pending" if terminal else remote_status
        now = utc_now()
        with self.database.transaction() as connection:
            batch = connection.execute("SELECT * FROM analysis_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            if (
                str(batch["status"]) != "submitting"
                or str(batch["submission_attempt_id"] or "") != attempt_id
            ):
                return None
            if str(batch["side_effect_owner"] or "") != owner:
                return None
            if batch["remote_batch_id"] not in (None, "", remote_id):
                raise ValueError("BATCH-REMOTE-IDENTITY-001 遠端 Batch ID 不可變更")
            other = connection.execute(
                "SELECT id FROM analysis_batches WHERE remote_batch_id=? AND id<>?",
                (remote_id, batch_id),
            ).fetchone()
            if other is not None:
                raise ValueError("BATCH-RECOVERY-006 遠端 Batch 已綁定其他本機 Batch")
            remote_input = remote.get("input_file_id")
            if remote_input is not None and str(remote_input) != str(batch["input_file_id"] or ""):
                raise ValueError("BATCH-REMOTE-IDENTITY-002 遠端 input_file_id 不可變更")
            for key in ("output_file_id", "error_file_id"):
                remote_value = remote.get(key)
                if remote_value is not None and batch[key] not in (None, "", remote_value):
                    raise ValueError(f"BATCH-REMOTE-IDENTITY-003 {key} 不可變更")
            assignments: dict[str, Any] = {
                "remote_batch_id": remote_id,
                "status": local_status,
                "remote_status": remote_status,
                "last_error_code": None,
                "last_error_message": None,
                "last_polled_at": now,
                "phase_started_at": now,
                "side_effect_owner": None,
                "side_effect_lease_until": None,
                "side_effect_version": int(batch["side_effect_version"] or 0) + 1,
                "updated_at": now,
            }
            for key in ("output_file_id", "error_file_id"):
                if remote.get(key) is not None:
                    assignments[key] = remote[key]
            if terminal:
                assignments["completed_at"] = (
                    remote.get("completed_at")
                    or remote.get("expired_at")
                    or remote.get("cancelled_at")
                    or now
                )
            values = [assignments[key] for key in assignments] + [batch_id, attempt_id, owner]
            cursor = connection.execute(
                f"UPDATE analysis_batches SET {','.join(f'{key}=?' for key in assignments)} "  # noqa: S608
                "WHERE id=? AND status='submitting' AND submission_attempt_id=? AND side_effect_owner=?",
                values,
            )
            if cursor.rowcount != 1:
                return None
            connection.execute(
                """
                UPDATE analysis_batch_items SET status='submitted',error_code=NULL,error_message=NULL,updated_at=?
                WHERE batch_id=? AND status IN ('pending','submission_unknown')
                """,
                (now, batch_id),
            )
        return local_status

    def mark_submission_unknown(
        self, batch_id: str, attempt_id: str, owner: str, error_code: str, error_message: str
    ) -> bool:
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_batches
                SET status='submission_unknown',remote_status='submission_unknown',
                    last_error_code=?,last_error_message=?,completed_at=NULL,phase_started_at=?,updated_at=?,
                    side_effect_owner=NULL,side_effect_lease_until=NULL,side_effect_version=side_effect_version+1
                WHERE id=? AND status='submitting' AND submission_attempt_id=? AND side_effect_owner=?
                """,
                (error_code, error_message[:1000], now, now, batch_id, attempt_id, owner),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE analysis_batch_items SET status='submission_unknown',error_code=?,error_message=?,updated_at=?
                    WHERE batch_id=? AND status IN ('pending','submitted')
                    """,
                    (error_code, error_message[:1000], now, batch_id),
                )
        return bool(cursor.rowcount)

    def recover_uploaded_file(
        self,
        batch_id: str,
        input_file_id: str,
        input_file_bytes: int | None = None,
        connection=None,
    ) -> bool:
        now = utc_now()
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            cursor = connection.execute(
                """
                UPDATE analysis_batches
                SET input_file_id=?,input_file_bytes=COALESCE(?,input_file_bytes),status='uploaded',
                    cleanup_status='pending',last_error_code=NULL,last_error_message=NULL,phase_started_at=?,
                    side_effect_owner=NULL,side_effect_lease_until=NULL,side_effect_version=side_effect_version+1,
                    updated_at=?
                WHERE id=? AND status='upload_unknown' AND remote_batch_id IS NULL
                """,
                (input_file_id, input_file_bytes, now, now, batch_id),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE analysis_batch_items SET status='pending',error_code=NULL,error_message=NULL,updated_at=?
                    WHERE batch_id=? AND status='upload_unknown'
                    """,
                    (now, batch_id),
                )
        return bool(cursor.rowcount)

    def abandon_unknown_upload(self, batch_id: str, *, confirmed_deleted: bool) -> bool:
        if not confirmed_deleted:
            raise ValueError("必須提供遠端 File 已刪除的明確證據")
        now = utc_now()
        with self.database.transaction() as connection:
            batch = connection.execute(
                "SELECT job_id,status FROM analysis_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            if str(batch["status"]) != "upload_unknown":
                raise ValueError("目前 Batch 不在 upload_unknown")
            cursor = connection.execute(
                """
                UPDATE analysis_batches SET status='failed',remote_status='abandoned',cleanup_status='completed',
                    cleanup_completed_at=?,completed_at=?,abandon_confirmed_at=?,last_error_code='abandoned',
                    last_error_message='管理員確認遠端 File 已刪除',input_file_id=NULL,
                    input_file_deleted=1,side_effect_owner=NULL,side_effect_lease_until=NULL,
                    side_effect_version=side_effect_version+1,updated_at=?
                WHERE id=? AND status='upload_unknown'
                """,
                (now, now, now, now, batch_id),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE analysis_batch_items SET status='failed',error_code='abandoned',
                        error_message='管理員確認遠端 File 已刪除',updated_at=?
                    WHERE batch_id=? AND status NOT IN ('imported','failed','cancelled')
                    """,
                    (now, batch_id),
                )
                if batch["job_id"]:
                    job_id = str(batch["job_id"])
                    connection.execute(
                        """
                        UPDATE job_items SET status='failed',completed_at=?,error_code='abandoned',lease_until=NULL
                        WHERE job_id=? AND status NOT IN ('completed','failed','cancelled')
                        """,
                        (now, job_id),
                    )
                    connection.execute(
                        """
                        UPDATE jobs SET status='failed',completed_at=?,completed_items=(
                            SELECT COUNT(*) FROM job_items WHERE job_id=? AND status='completed'
                        ),failed_items=(
                            SELECT COUNT(*) FROM job_items WHERE job_id=? AND status='failed'
                        ),heartbeat_at=?
                        WHERE id=? AND status NOT IN ('completed','completed_with_errors','failed','cancelled')
                        """,
                        (now, job_id, job_id, now, job_id),
                    )
        return bool(cursor.rowcount)

    def fail_local_batch(
        self,
        batch_id: str,
        error_code: str,
        error_message: str,
        *,
        job_status: str = "failed",
        include_job_siblings: bool = False,
        connection=None,
    ) -> bool:
        """Atomically fail a local-only Batch and release every reservation."""

        if job_status not in {"failed", "completed_with_errors"}:
            raise ValueError("不合法的 local Batch Job terminal status")
        now = utc_now()
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            batch = connection.execute("SELECT * FROM analysis_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            job_id = str(batch["job_id"] or "")
            if include_job_siblings and job_id:
                target_rows = connection.execute(
                    "SELECT * FROM analysis_batches WHERE job_id=? ORDER BY id", (job_id,)
                ).fetchall()
            else:
                target_rows = [batch]
            if any(
                row[key]
                for row in target_rows
                for key in ("input_file_id", "remote_batch_id", "output_file_id", "error_file_id")
            ):
                raise ValueError("已有遠端 side effect，不可使用 local failure release")
            target_ids = [str(row["id"]) for row in target_rows]
            if not target_ids:
                return False
            placeholders = ",".join("?" for _ in target_ids)
            cursor = connection.execute(
                f"""
                UPDATE analysis_batches SET status='failed',remote_status='failed',cleanup_status='not_required',
                    cleanup_completed_at=?,completed_at=?,failed_items=total_items,missing_items=0,stale_items=0,
                    imported_items=0,last_error_code=?,last_error_message=?,
                    side_effect_owner=NULL,side_effect_lease_until=NULL,side_effect_version=side_effect_version+1,
                    updated_at=?
                WHERE id IN ({placeholders}) AND remote_batch_id IS NULL AND input_file_id IS NULL
                """,  # noqa: S608 -- placeholders are generated from trusted local IDs.
                (now, now, error_code, error_message[:1000], now, *target_ids),
            )
            if cursor.rowcount != len(target_ids):
                return False
            connection.execute(
                f"""
                UPDATE analysis_batch_items SET status='failed',error_code=?,error_message=?,updated_at=?
                WHERE batch_id IN ({placeholders}) AND status NOT IN ('imported','failed','cancelled')
                """,  # noqa: S608 -- placeholders are generated from trusted local IDs.
                (error_code, error_message[:1000], now, *target_ids),
            )
            if job_id:
                connection.execute(
                    f"""
                    UPDATE job_items SET status='failed',completed_at=?,error_code=?,lease_until=NULL
                    WHERE id IN (
                        SELECT job_item_id FROM analysis_batch_items
                        WHERE batch_id IN ({placeholders}) AND job_item_id IS NOT NULL
                    ) AND status NOT IN ('completed','failed','cancelled')
                    """,  # noqa: S608 -- placeholders are generated from trusted local IDs.
                    (now, error_code, *target_ids),
                )
                aggregates = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),0) AS completed,
                        COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed
                    FROM job_items WHERE job_id=?
                    """,
                    (job_id,),
                ).fetchone()
                pending_batches = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM analysis_batches WHERE job_id=? AND status NOT IN ({','.join('?' for _ in TERMINAL_BATCH_STATUSES)})",  # noqa: S608
                        (job_id, *sorted(TERMINAL_BATCH_STATUSES)),
                    ).fetchone()[0]
                )
                if pending_batches == 0:
                    connection.execute(
                        """
                        UPDATE jobs SET status=?,completed_at=?,completed_items=?,failed_items=?,heartbeat_at=?
                        WHERE id=? AND status!='cancelled'
                        """,
                        (
                            job_status,
                            now,
                            int(aggregates["completed"] or 0),
                            int(aggregates["failed"] or 0),
                            now,
                            job_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE jobs SET completed_items=?,failed_items=?,heartbeat_at=?
                        WHERE id=? AND status!='cancelled'
                        """,
                        (
                            int(aggregates["completed"] or 0),
                            int(aggregates["failed"] or 0),
                            now,
                            job_id,
                        ),
                    )
        return True

    def set_status_from_remote(
        self,
        batch_id: str,
        remote: dict[str, Any],
        *,
        expected_version: int | None = None,
        owner: str | None = None,
    ) -> str:
        """Apply a remote status only if it is newer than the local claim.

        A stale poll response is a normal race outcome, not an exception.  It
        returns ``ignored_stale`` and cannot clear completion timestamps or
        rewrite remote file identity.
        """

        status = str(remote.get("status") or "").strip()
        allowed = {
            "validating",
            "in_progress",
            "finalizing",
            "completed",
            "failed",
            "expired",
            "cancelling",
            "cancelled",
        }
        if status not in allowed:
            raise ValueError("BATCH-REMOTE-001 遠端 Batch 缺少有效 status")
        ranks = {
            "validating": 10,
            "in_progress": 20,
            "finalizing": 30,
            "cancelling": 35,
            "completed": 40,
            "failed": 40,
            "expired": 40,
            "cancelled": 40,
        }
        terminal_remote = {"completed", "failed", "expired", "cancelled"}
        now = utc_now()
        with self.database.transaction() as connection:
            current = connection.execute("SELECT * FROM analysis_batches WHERE id=?", (batch_id,)).fetchone()
            if current is None:
                raise KeyError(batch_id)
            current_local = str(current["status"])
            current_remote = str(current["remote_status"] or "")
            current_version = int(current["side_effect_version"] or 0)
            if expected_version is None:
                expected_version = current_version
            if current_version != int(expected_version):
                return "ignored_stale"
            if owner is not None and str(current["side_effect_owner"] or "") != owner:
                return "ignored_stale"
            if current_local in {"import_pending", "importing", "cleanup_pending"} | TERMINAL_BATCH_STATUSES:
                return "ignored_stale"
            if current_remote in ranks and ranks[current_remote] > ranks[status]:
                return "ignored_stale"
            if current_remote in terminal_remote and status != current_remote:
                return "ignored_stale"
            if current_remote in terminal_remote and status not in terminal_remote:
                return "ignored_stale"
            if current_local == "cancelling" and status in {"validating", "in_progress", "finalizing"}:
                return "ignored_stale"
            remote_id = remote.get("id") or remote.get("remote_batch_id")
            if remote_id is not None and str(current["remote_batch_id"] or "") not in {"", str(remote_id)}:
                raise ValueError("BATCH-REMOTE-IDENTITY-001 遠端 Batch ID 不可變更")
            remote_input = remote.get("input_file_id")
            if remote_input is not None and str(current["input_file_id"] or "") not in {
                "",
                str(remote_input),
            }:
                raise ValueError("BATCH-REMOTE-IDENTITY-002 遠端 input_file_id 不可變更")
            for key in ("output_file_id", "error_file_id"):
                remote_value = remote.get(key)
                local_value = current[key]
                if remote_value is not None and local_value not in (None, "", remote_value):
                    raise ValueError(f"BATCH-REMOTE-IDENTITY-003 {key} 不可變更")
            counts = remote.get("request_counts") or {}
            next_status = "import_pending" if status in terminal_remote else status
            changes: dict[str, Any] = {
                "status": next_status,
                "last_polled_at": now,
                "remote_status": status,
                "side_effect_owner": None,
                "side_effect_lease_until": None,
                "side_effect_version": current_version + 1,
                "updated_at": now,
            }
            if status in terminal_remote and not current["completed_at"]:
                changes["completed_at"] = (
                    remote.get("completed_at")
                    or remote.get("expired_at")
                    or remote.get("cancelled_at")
                    or now
                )
            for key in ("remote_batch_id", "output_file_id", "error_file_id"):
                value = remote.get(key)
                if value is not None:
                    changes[key] = value
            if remote_input is not None and not current["input_file_id"]:
                changes["input_file_id"] = remote_input
            if "total" in counts:
                changes["total_items"] = max(int(current["total_items"] or 0), int(counts.get("total") or 0))
            if "completed" in counts:
                changes["completed_items"] = max(
                    int(current["completed_items"] or 0), int(counts.get("completed") or 0)
                )
            if "failed" in counts:
                changes["failed_items"] = max(
                    int(current["failed_items"] or 0), int(counts.get("failed") or 0)
                )
            errors = remote.get("errors")
            if errors:
                changes["last_error_code"] = "remote_validation_error"
                changes["last_error_message"] = str(errors)[:1000]
            assignments = ",".join(f"{key}=?" for key in changes)
            values = [changes[key] for key in changes] + [batch_id, current_version]
            cursor = connection.execute(
                f"UPDATE analysis_batches SET {assignments} WHERE id=? AND side_effect_version=?",  # noqa: S608
                values,
            )
            if cursor.rowcount != 1:
                return "ignored_stale"
            connection.execute(
                """
                UPDATE analysis_batch_items
                SET status='submitted',error_code=NULL,error_message=NULL,updated_at=?
                WHERE batch_id=? AND status IN ('pending','submission_unknown')
                """,
                (now, batch_id),
            )
            return next_status

    def bind_recovered_remote(
        self,
        batch_id: str,
        remote: dict[str, Any],
        *,
        item_status: str = "submitted",
        connection=None,
    ) -> str:
        """Atomically bind an already-existing remote Batch after ownership checks."""

        remote_id = str(remote.get("id") or remote.get("remote_batch_id") or "")
        if not remote_id:
            raise ValueError("BATCH-RECOVERY-004 遠端 Batch 缺少 id")
        status = str(remote.get("status") or "")
        terminal = {"completed", "failed", "expired", "cancelled"}
        local_status = "import_pending" if status in terminal else status
        if local_status not in {"import_pending", "validating", "in_progress", "finalizing", "cancelling"}:
            raise ValueError("BATCH-RECOVERY-005 遠端 Batch 狀態不支援")
        now = utc_now()
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as active_connection:
            connection = active_connection
            batch = connection.execute("SELECT * FROM analysis_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            if str(batch["status"]) not in {"submission_unknown", "submitting", "validating"}:
                raise ValueError("BATCH-RECOVERY-002 Batch 不在可 Recovery 的提交狀態")
            other = connection.execute(
                "SELECT id FROM analysis_batches WHERE remote_batch_id=? AND id<>?",
                (remote_id, batch_id),
            ).fetchone()
            if other is not None:
                raise ValueError("BATCH-RECOVERY-006 遠端 Batch 已綁定其他本機 Batch")
            if batch["remote_batch_id"] not in (None, "", remote_id):
                raise ValueError("BATCH-RECOVERY-003 Batch 已綁定其他遠端 ID")
            counts = remote.get("request_counts") or {}
            assignments: dict[str, Any] = {
                "remote_batch_id": remote_id,
                "status": local_status,
                "remote_status": status,
                "last_error_code": None,
                "last_error_message": None,
                "phase_started_at": now,
                "last_polled_at": now,
                "side_effect_owner": None,
                "side_effect_lease_until": None,
                "side_effect_version": int(batch["side_effect_version"] or 0) + 1,
            }
            if status in terminal:
                assignments["completed_at"] = (
                    remote.get("completed_at") or remote.get("expired_at") or remote.get("cancelled_at")
                )
            else:
                assignments["completed_at"] = None
            for key in ("output_file_id", "error_file_id"):
                if remote.get(key) is not None:
                    if batch[key] not in (None, "", remote[key]):
                        raise ValueError(f"BATCH-RECOVERY-008 {key} 不可變更")
                    assignments[key] = remote[key]
            if "total" in counts:
                assignments["total_items"] = max(0, int(counts.get("total") or 0))
            assignments["updated_at"] = now
            values = [assignments[key] for key in assignments] + [batch_id]
            connection.execute(
                f"UPDATE analysis_batches SET {','.join(f'{key}=?' for key in assignments)} WHERE id=?",  # noqa: S608
                values,
            )
            connection.execute(
                "UPDATE analysis_batch_items SET status=?,error_code=NULL,error_message=NULL,updated_at=? "
                "WHERE batch_id=? AND status IN ('pending','upload_unknown','submission_unknown')",
                (item_status, now, batch_id),
            )
        return local_status

    def mark_file_deleted(self, batch_id: str, file_kind: str) -> None:
        columns = {
            "input": "input_file_deleted",
            "output": "output_file_deleted",
            "error": "error_file_deleted",
        }
        column = columns.get(file_kind)
        if column is None:
            raise ValueError("不支援的 Batch remote file 類型")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE analysis_batches SET {column}=1,updated_at=? WHERE id=?",  # noqa: S608
                (utc_now(), batch_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(batch_id)

    def claim_cleanup_file(
        self, batch_id: str, file_kind: str, owner: str, lease_until: str
    ) -> tuple[str, bool] | None:
        columns = {
            "input": ("input_file_id", "input_file_deleted"),
            "output": ("output_file_id", "output_file_deleted"),
            "error": ("error_file_id", "error_file_deleted"),
        }
        file_column, deleted_column = columns.get(file_kind, (None, None))
        if file_column is None or deleted_column is None:
            raise ValueError("不支援的 Batch remote file 類型")
        now = utc_now()
        with self.database.transaction() as connection:
            before = connection.execute(
                f"SELECT {file_column},last_error_code FROM analysis_batches WHERE id=? AND {file_column} IS NOT NULL AND {deleted_column}=0",  # noqa: S608
                (batch_id,),
            ).fetchone()
            if before is None:
                return None
            cursor = connection.execute(
                f"""
                UPDATE analysis_batches
                SET side_effect_owner=?,side_effect_lease_until=?,side_effect_version=side_effect_version+1,
                    last_error_code=?,last_error_message=NULL,updated_at=?
                WHERE id=? AND {file_column} IS NOT NULL AND {deleted_column}=0
                  AND (side_effect_lease_until IS NULL OR side_effect_lease_until<=?)
                """,  # noqa: S608
                (
                    owner,
                    lease_until,
                    f"cleanup_delete_unknown:{file_kind}",
                    now,
                    batch_id,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return str(before[file_column]), str(before["last_error_code"] or "") == (
                f"cleanup_delete_unknown:{file_kind}"
            )

    def complete_cleanup_file(self, batch_id: str, file_kind: str, owner: str) -> bool:
        columns = {
            "input": "input_file_deleted",
            "output": "output_file_deleted",
            "error": "error_file_deleted",
        }
        column = columns.get(file_kind)
        if column is None:
            raise ValueError("不支援的 Batch remote file 類型")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE analysis_batches SET {column}=1,side_effect_owner=NULL,side_effect_lease_until=NULL,
                    side_effect_version=side_effect_version+1,last_error_code=NULL,last_error_message=NULL,updated_at=?
                WHERE id=? AND side_effect_owner=?
                """,  # noqa: S608
                (utc_now(), batch_id, owner),
            )
        return bool(cursor.rowcount)

    def fail_cleanup_file(self, batch_id: str, owner: str, error_code: str, error_message: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_batches
                SET side_effect_owner=NULL,side_effect_lease_until=NULL,side_effect_version=side_effect_version+1,
                    last_error_code=?,last_error_message=?,updated_at=?
                WHERE id=? AND side_effect_owner=?
                """,
                (error_code, error_message[:1000], utc_now(), batch_id, owner),
            )
        return bool(cursor.rowcount)

    def counts(self, batch_id: str) -> dict[str, int]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM analysis_batch_items WHERE batch_id=? GROUP BY status",
                (batch_id,),
            ).fetchall()
        result = {str(row["status"]): int(row["count"]) for row in rows}
        return result

    @staticmethod
    def snapshot_json(photo_ids: Iterable[str]) -> str:
        return json.dumps(list(photo_ids), ensure_ascii=False, separators=(",", ":"))
