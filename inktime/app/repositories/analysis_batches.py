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
        "phase_started_at",
        "abandon_confirmed_at",
        "input_file_id",
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
                    peak_rss_bytes,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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

    def set_status_from_remote(self, batch_id: str, remote: dict[str, Any]) -> str:
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
        counts = remote.get("request_counts") or {}
        current = self.get(batch_id)
        if current is None:
            raise KeyError(batch_id)
        remote_id = remote.get("id") or remote.get("remote_batch_id")
        if remote_id is not None and str(current["remote_batch_id"] or "") not in {"", str(remote_id)}:
            raise ValueError("BATCH-REMOTE-IDENTITY-001 遠端 Batch ID 不可變更")
        remote_input = remote.get("input_file_id")
        if remote_input is not None and str(current["input_file_id"] or "") not in {"", str(remote_input)}:
            raise ValueError("BATCH-REMOTE-IDENTITY-002 遠端 input_file_id 不可變更")
        for key in ("output_file_id", "error_file_id"):
            remote_value = remote.get(key)
            local_value = current[key]
            if remote_value is not None and local_value not in (None, "", remote_value):
                raise ValueError(f"BATCH-REMOTE-IDENTITY-003 {key} 不可變更")
        changes: dict[str, Any] = {
            "status": "import_pending"
            if status in {"completed", "failed", "expired", "cancelled"}
            else status,
            "last_polled_at": utc_now(),
            "completed_at": remote.get("completed_at")
            or remote.get("expired_at")
            or remote.get("cancelled_at"),
            "remote_status": status,
        }
        for key in ("remote_batch_id", "output_file_id", "error_file_id"):
            value = remote.get(key)
            if value is not None:
                changes[key] = value
        if remote_input is not None and not current["input_file_id"]:
            changes["input_file_id"] = remote_input
        if "total" in counts:
            changes["total_items"] = max(0, int(counts.get("total") or 0))
        if "completed" in counts:
            changes["completed_items"] = max(0, int(counts.get("completed") or 0))
        if "failed" in counts:
            changes["failed_items"] = max(0, int(counts.get("failed") or 0))
        errors = remote.get("errors")
        if errors:
            changes["last_error_code"] = "remote_validation_error"
            changes["last_error_message"] = str(errors)[:1000]
        self.update_batch(batch_id, **changes)
        return str(changes["status"])

    def bind_recovered_remote(
        self, batch_id: str, remote: dict[str, Any], *, item_status: str = "submitted"
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
        with self.database.transaction() as connection:
            batch = connection.execute("SELECT * FROM analysis_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError(batch_id)
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
            }
            if status in terminal:
                assignments["completed_at"] = (
                    remote.get("completed_at") or remote.get("expired_at") or remote.get("cancelled_at")
                )
            for key in ("output_file_id", "error_file_id"):
                if remote.get(key) is not None:
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
