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
        "input_file_id",
        "remote_batch_id",
        "output_file_id",
        "error_file_id",
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

    def update_batch(self, batch_id: str, **changes: Any) -> None:
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
        with self.database.session() as connection:
            cursor = connection.execute(
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
        for key in ("input_file_id", "remote_batch_id", "output_file_id", "error_file_id"):
            value = remote.get(key)
            if value is not None:
                changes[key] = value
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
