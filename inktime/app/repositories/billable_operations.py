from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
import json
import hashlib
from uuid import uuid4

from inktime.app.db import Database
from inktime.app.providers.base import ProviderResponse, deserialize_response
from inktime.app.repositories.analysis_reservations import assert_no_batch_content_owner
from inktime.app.repositories.model_evidence import has_valid_saved_photo_response


class UnreconciledOperationError(RuntimeError):
    code = "VLM-UNRECONCILED"
    ambiguous = True


class SeasonalAlreadyProcessedError(RuntimeError):
    code = "SEASONAL-ALREADY-PROCESSED"
    not_sent = True


class SavedModelResultAvailableError(RuntimeError):
    """A result appeared after the local reuse check, before a new request."""

    not_sent = True


def paid_operation_lock_name(content_key: str) -> str:
    # A Provider contract can hold its diagnostic lock while sending a paid
    # subrequest. Separate the namespaces so their lock shards cannot collide.
    prefix = "paid-diagnostic-" if content_key.startswith("diagnostic:") else "paid-operation-"
    return prefix + hashlib.sha256(content_key.encode()).hexdigest()[:3]


def approved_receipt_sql(content_sha: str) -> str:
    """Find old approvals which still carry a possibly usable response."""
    return f"""EXISTS(
        SELECT 1 FROM billable_operations approved
        LEFT JOIN model_request_evidence evidence ON evidence.operation_id=approved.id
        WHERE approved.content_sha256={content_sha} AND approved.state='approved'
          AND COALESCE(approved.response_json,evidence.response_json) IS NOT NULL
    )"""  # noqa: S608 -- fixed column expressions supplied by repository callers.


class BillableOperationRepository:
    """A paid request outlives worker leases; a crash must never authorize resend."""

    def __init__(self, database: Database):
        self.database = database

    _PHOTO_RECEIPT_SELECT = """
        SELECT o.*,e.request_json,e.response_json AS transport_response,
               original_job.id AS original_job_id,original_job.analysis_spec_json AS original_job_plan,
               original_photo.id AS original_photo_id,reservation.provider_id AS reserved_provider_id
        FROM billable_operations o
        LEFT JOIN model_request_evidence e ON e.operation_id=o.id
        LEFT JOIN jobs original_job ON original_job.id=json_extract(
            CASE WHEN json_valid(o.resolution_note) THEN o.resolution_note
                 WHEN instr(o.resolution_note,char(10))>0 AND
                      json_valid(substr(o.resolution_note,1,instr(o.resolution_note,char(10))-1))
                 THEN substr(o.resolution_note,1,instr(o.resolution_note,char(10))-1) ELSE '{}' END, '$.job_id')
        LEFT JOIN photos original_photo ON original_photo.id=json_extract(
            CASE WHEN json_valid(o.resolution_note) THEN o.resolution_note
                 WHEN instr(o.resolution_note,char(10))>0 AND
                      json_valid(substr(o.resolution_note,1,instr(o.resolution_note,char(10))-1))
                 THEN substr(o.resolution_note,1,instr(o.resolution_note,char(10))-1) ELSE '{}' END, '$.photo_id')
        LEFT JOIN budget_reservations reservation ON reservation.id='vision:'||o.id
    """

    def _approved_photo_response(self, connection, content_sha256: str) -> dict | None:
        # Old builds could approve a resend despite an already valid answer.
        for row in connection.execute(
            self._PHOTO_RECEIPT_SELECT + """
            WHERE o.content_sha256=? AND o.state='approved'
              AND COALESCE(o.response_json,e.response_json) IS NOT NULL
            ORDER BY o.created_at DESC,o.id DESC
            """,
            (content_sha256,),
        ):
            saved = dict(row)
            if has_valid_saved_photo_response(saved):
                return saved
        return None

    def has_approved_photo_response(self, content_sha256: str, *, connection=None) -> bool:
        context = self.database.session() if connection is None else nullcontext(connection)
        with context as active:
            return self._approved_photo_response(active, content_sha256) is not None

    def photo_operation(
        self, content_sha256: str, *, include_completed: bool, job_id: str | None = None,
    ) -> dict | None:
        with self.database.session() as connection:
            row = connection.execute(
                self._PHOTO_RECEIPT_SELECT + """
                WHERE o.content_sha256=?
                  AND (o.state IN ('started','response')
                    OR (o.state='completed' AND (? OR original_job.id=?)))
                ORDER BY CASE WHEN o.state='completed' THEN 1 ELSE 0 END,o.created_at DESC,o.id DESC
                LIMIT 1
                """,
                (content_sha256, int(include_completed), job_id),
            ).fetchone()
            if row is not None:
                return dict(row)
            return self._approved_photo_response(connection, content_sha256) if include_completed else None

    def check_retry(
        self, content_sha256: str, request_fingerprint: str, *, reuse_completed: bool = False,
        job_id: str | None = None,
    ) -> bool:
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
                "AND (state='response' OR (state='completed' AND (? OR "
                "json_extract(CASE WHEN json_valid(resolution_note) THEN resolution_note ELSE '{}' END,'$.job_id')=?))) "
                "LIMIT 1",
                (request_fingerprint, int(reuse_completed), job_id),
            ).fetchone() is not None
            if not saved:
                assert_no_batch_content_owner(connection, content_sha256)
            return saved

    def begin(
        self, content_sha256: str, request_fingerprint: str, *, reuse_completed: bool = False,
        context: dict | None = None, guard_paid_photo: bool = False,
    ) -> tuple[str, ProviderResponse | None]:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            prior = connection.execute(
                "SELECT content_sha256,request_signature FROM billable_operations WHERE request_fingerprint=? "
                "ORDER BY created_at DESC LIMIT 1",
                (request_fingerprint,),
            ).fetchone()
            if prior and str(prior["content_sha256"]) != content_sha256:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            if prior and (context or {}).get("request_signature"):
                if prior["request_signature"] != (context or {}).get("request_signature"):
                    raise ValueError("IDEMPOTENCY_CONFLICT")
            unknown = connection.execute(
                "SELECT id FROM billable_operations WHERE content_sha256=? AND state='started' LIMIT 1",
                (content_sha256,),
            ).fetchone()
            if unknown:
                raise UnreconciledOperationError("先前 Vision 請求狀態未知；須先對帳，不得自動重新送圖")
            job_id = (context or {}).get("job_id") if guard_paid_photo else None
            saved = connection.execute(
                "SELECT id,response_json FROM billable_operations WHERE request_fingerprint=? "
                "AND (state='response' OR (state='completed' AND (? OR "
                "json_extract(CASE WHEN json_valid(resolution_note) THEN resolution_note ELSE '{}' END,'$.job_id')=?))) "
                "ORDER BY created_at DESC LIMIT 1",
                (request_fingerprint, int(reuse_completed), job_id),
            ).fetchone()
            if saved:
                return str(saved["id"]), deserialize_response(saved["response_json"])
            backfill = connection.execute(
                "SELECT 1 FROM jobs WHERE id=? AND json_extract(settings_json,'$.missing_analysis_backfill')=1",
                ((context or {}).get("job_id"),),
            ).fetchone()
            if backfill and connection.execute(
                "SELECT 1 FROM model_analysis_results WHERE content_sha256=?", (content_sha256,),
            ).fetchone():
                raise SeasonalAlreadyProcessedError("相同照片內容已有完整模型資訊，補齊工作沿用原結果")
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
            if guard_paid_photo and reuse_completed and (connection.execute(
                "SELECT 1 FROM model_analysis_results WHERE content_sha256=? "
                "UNION ALL SELECT 1 FROM billable_operations WHERE content_sha256=? "
                "AND state IN ('response','completed') LIMIT 1",
                (content_sha256, content_sha256),
            ).fetchone() or self._approved_photo_response(connection, content_sha256) is not None):
                raise SavedModelResultAvailableError("相同照片已保存模型結果，改為讀取本機紀錄")
            if job_id is not None and not reuse_completed and connection.execute(
                "SELECT 1 FROM billable_operations WHERE content_sha256=? AND state='completed' "
                "AND json_extract(CASE WHEN json_valid(resolution_note) THEN resolution_note ELSE '{}' END,'$.job_id')=?",
                (content_sha256, job_id),
            ).fetchone():
                raise SavedModelResultAvailableError("這份重算工作已保存模型結果，改為讀取本機紀錄")
            assert_no_batch_content_owner(connection, content_sha256)
            operation_id = str(uuid4())
            connection.execute(
                "INSERT INTO billable_operations(id,content_sha256,request_fingerprint,state,created_at,updated_at,resolution_note,request_signature) "
                "VALUES (?,?,?,'started',?,?,?,?)", (operation_id, content_sha256, request_fingerprint, now, now,
                    json.dumps(context, ensure_ascii=False) if context else None, (context or {}).get("request_signature")),
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

    def finish(self, operation_id: str, *, not_sent: bool = False, connection=None) -> None:
        context = self.database.transaction() if connection is None else nullcontext(connection)
        with context as connection:
            connection.execute(
                "UPDATE billable_operations SET state=?,updated_at=? WHERE id=?",
                ("not_sent" if not_sent else "completed", datetime.now(timezone.utc).isoformat(), operation_id),
            )

    def finish_rejected(self, operation_id: str, error: Exception | None = None) -> None:
        """Close a definite OpenAI 4xx rejection, retaining sent-request evidence.

        The legacy not_sent state also releases a call the provider explicitly
        refused to execute. It must never be used for a timeout or generic 5xx.
        """
        status = (getattr(error, "http_status", None)
                  or getattr(getattr(error, "call_trace", None), "http_status", None))
        note = "OpenAI definitive HTTP rejection; model execution was not accepted"
        if error is not None:
            note += f"; HTTP {status}; code={str(getattr(error, 'code', type(error).__name__))[:80]}"
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE billable_operations SET state='not_sent',resolution_note=?,updated_at=? "
                "WHERE id=? AND state='started'",
                (note,
                 datetime.now(timezone.utc).isoformat(), operation_id),
            )

    def approve_resend(self, operation_id: str, *, reason: str) -> None:
        """Explicit operator decision; retain unknown billing and the original evidence."""
        if not reason.strip():
            raise ValueError("重送批准必須記錄原因")
        with self.database.session() as connection:
            operation = connection.execute("SELECT content_sha256 FROM billable_operations WHERE id=?", (operation_id,)).fetchone()
        if operation is None:
            raise ValueError("找不到付費操作")
        lock = self.database.try_acquire_operation_lock(paid_operation_lock_name(str(operation[0])))
        if lock is None:
            raise ValueError("模型請求仍由執行者處理中，不可批准重送")
        try:
            with self.database.transaction() as connection:
                saved = connection.execute(
                    self._PHOTO_RECEIPT_SELECT + " WHERE o.id=? AND o.state IN ('started','response')",
                    (operation_id,),
                ).fetchone()
                if saved is not None and not str(saved["content_sha256"]).startswith("diagnostic:") and (
                    has_valid_saved_photo_response(dict(saved))
                ):
                    raise ValueError("此照片已有可用的模型回應，請恢復已保存結果")
                cursor = connection.execute(
                    "UPDATE billable_operations SET state='approved',resolution_note=COALESCE(resolution_note || char(10),'') || ?,updated_at=? "
                    "WHERE id=? AND state IN ('started','response')",
                    (reason.strip()[:2000], datetime.now(timezone.utc).isoformat(), operation_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("找不到待對帳或回應待處理操作，或該操作已處理")
        finally:
            lock.close()
