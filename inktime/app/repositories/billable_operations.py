from dataclasses import asdict
from datetime import datetime, timezone
import json
from uuid import uuid4

from inktime.app.db import Database
from inktime.app.providers.base import ProviderCallTrace, ProviderResponse, Usage
from inktime.app.repositories.analysis_reservations import assert_no_batch_content_owner


class UnreconciledOperationError(RuntimeError):
    code = "VLM-UNRECONCILED"
    ambiguous = True


class SeasonalAlreadyProcessedError(RuntimeError):
    code = "SEASONAL-ALREADY-PROCESSED"
    not_sent = True


class BillableOperationRepository:
    """A paid request outlives worker leases; a crash must never authorize resend."""

    def __init__(self, database: Database):
        self.database = database

    def check_retry(self, content_sha256: str, request_fingerprint: str, *, reuse_completed: bool = False) -> bool:
        with self.database.session() as connection:
            if connection.execute(
                "SELECT 1 FROM billable_operations o LEFT JOIN ai_cache_reservations r "
                "ON r.cache_key=o.request_fingerprint WHERE o.content_sha256=? AND o.state='started' "
                "AND (r.cache_key IS NULL OR r.status<>'reserved' OR r.lease_until<=?) LIMIT 1",
                (content_sha256, datetime.now(timezone.utc).isoformat()),
            ).fetchone():
                raise UnreconciledOperationError("先前 Vision 請求狀態未知；須先對帳，不得自動重新送圖")
            saved = connection.execute(
                "SELECT 1 FROM billable_operations WHERE request_fingerprint=? "
                "AND (state='response' OR (? AND state='completed')) LIMIT 1",
                (request_fingerprint, int(reuse_completed)),
            ).fetchone() is not None
            if not saved:
                assert_no_batch_content_owner(connection, content_sha256)
            return saved

    def begin(self, content_sha256: str, request_fingerprint: str, *, reuse_completed: bool = False, context: dict | None = None) -> tuple[str, ProviderResponse | None]:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            prior = connection.execute(
                "SELECT content_sha256 FROM billable_operations WHERE request_fingerprint=? LIMIT 1",
                (request_fingerprint,),
            ).fetchone()
            if prior and str(prior["content_sha256"]) != content_sha256:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            unknown = connection.execute(
                "SELECT id FROM billable_operations WHERE content_sha256=? AND state='started' LIMIT 1",
                (content_sha256,),
            ).fetchone()
            if unknown:
                raise UnreconciledOperationError("先前 Vision 請求狀態未知；須先對帳，不得自動重新送圖")
            saved = connection.execute(
                "SELECT id,response_json FROM billable_operations WHERE request_fingerprint=? "
                "AND (state='response' OR (? AND state='completed')) ORDER BY created_at DESC LIMIT 1",
                (request_fingerprint, int(reuse_completed)),
            ).fetchone()
            if saved:
                payload = json.loads(saved["response_json"])
                payload["usage"] = Usage(**payload["usage"])
                if payload.get("call_trace"):
                    payload["call_trace"] = ProviderCallTrace(**payload["call_trace"])
                return str(saved["id"]), ProviderResponse(**payload)
            # Recheck at the last durable boundary, not only when candidates
            # were selected. An older job/provider can finish in between.
            seasonal = connection.execute(
                "SELECT j.force_recompute,json_extract(j.settings_json,'$.seasonal_retry_task_id') AS retry_task "
                "FROM seasonal_analysis_tasks t JOIN jobs j ON j.id=t.job_id "
                "WHERE t.job_id=? AND t.content_sha256=? LIMIT 1",
                ((context or {}).get("job_id"), content_sha256),
            ).fetchone()
            if seasonal is not None and not seasonal["force_recompute"] and not seasonal["retry_task"] and connection.execute(
                "SELECT 1 FROM billable_operations WHERE content_sha256=? AND state IN ('response','completed') LIMIT 1",
                (content_sha256,),
            ).fetchone():
                raise SeasonalAlreadyProcessedError("相同內容已有付費回應，沿用原資料；不因模型規格不同再次送出")
            assert_no_batch_content_owner(connection, content_sha256)
            operation_id = str(uuid4())
            connection.execute(
                "INSERT INTO billable_operations(id,content_sha256,request_fingerprint,state,created_at,updated_at,resolution_note) "
                "VALUES (?,?,?,'started',?,?,?)", (operation_id, content_sha256, request_fingerprint, now, now,
                                                      json.dumps(context, ensure_ascii=False) if context else None),
            )
            return operation_id, None

    def save_response(self, operation_id: str, response: ProviderResponse) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE billable_operations SET state='response',response_json=?,updated_at=? WHERE id=?",
                (json.dumps(asdict(response), ensure_ascii=False), datetime.now(timezone.utc).isoformat(), operation_id),
            )

    def is_completed(self, operation_id: str) -> bool:
        with self.database.session() as connection:
            row = connection.execute("SELECT state FROM billable_operations WHERE id=?", (operation_id,)).fetchone()
        return row is not None and row["state"] == "completed"

    def finish(self, operation_id: str, *, not_sent: bool = False) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE billable_operations SET state=?,updated_at=? WHERE id=?",
                ("not_sent" if not_sent else "completed", datetime.now(timezone.utc).isoformat(), operation_id),
            )

    def approve_resend(self, operation_id: str, *, reason: str) -> None:
        """Explicit operator decision; retain unknown billing and the original evidence."""
        if not reason.strip():
            raise ValueError("重送批准必須記錄原因")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE billable_operations SET state='approved',resolution_note=COALESCE(resolution_note || char(10),'') || ?,updated_at=? "
                "WHERE id=? AND state IN ('started','response')",
                (reason.strip()[:2000], datetime.now(timezone.utc).isoformat(), operation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("找不到待對帳或回應待處理操作，或該操作已處理")
