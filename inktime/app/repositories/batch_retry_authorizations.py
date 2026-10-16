"""Explicit, item-scoped authorization for the existing Retry Failed action."""
from datetime import datetime, timezone
from uuid import uuid4

from inktime.app.repositories.analysis_reservations import AnalysisReservationConflict, held_batch_sql


class BatchRetryAuthorizationRepository:
    def __init__(self, database):
        self.database = database

    def create(self, batch_id, *, approved_by):
        token = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            batch = connection.execute("SELECT * FROM analysis_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            if connection.execute(
                f"SELECT 1 FROM analysis_batches reserved_batch WHERE id=? AND {held_batch_sql()}",  # noqa: S608
                (batch_id,),
            ).fetchone():
                raise AnalysisReservationConflict("原 Batch 仍在執行或遠端狀態未知；必須先回收或對帳，不能批准重送")
            failed = connection.execute(
                "SELECT id,photo_id,content_sha256 FROM analysis_batch_items WHERE batch_id=? "
                "AND photo_id IS NOT NULL AND status IN ('failed','missing','retry_pending','schema_invalid',"
                "'duplicate_custom_id','unexpected_custom_id') AND COALESCE(error_code,'')<>'submission_unknown'",
                (batch_id,),
            ).fetchall()
            if not failed:
                raise ValueError("此 Batch 沒有可重試項目")
            connection.executemany(
                "INSERT INTO analysis_batch_retry_authorizations(authorization_id,source_item_id,content_sha256,"
                "approved_by,created_at) VALUES (?,?,?,?,?)",
                ((token, item["id"], item["content_sha256"], approved_by, now) for item in failed),
            )
            # Carry only ancestors of the selected failed bytes. A later retry
            # cannot authorize a successful sibling or unrelated paid request.
            connection.execute(
                "INSERT OR IGNORE INTO analysis_batch_retry_authorizations(authorization_id,source_item_id,"
                "content_sha256,approved_by,created_at) "
                "SELECT ?,ancestor.source_item_id,ancestor.content_sha256,?,? "
                "FROM analysis_batch_retry_authorizations ancestor "
                "WHERE ancestor.retry_job_id=? AND EXISTS("
                "SELECT 1 FROM analysis_batch_retry_authorizations selected "
                "WHERE selected.authorization_id=? AND selected.content_sha256=ancestor.content_sha256)",
                (token, approved_by, now, batch["job_id"], token),
            )
        return token, list(dict.fromkeys(str(item["photo_id"]) for item in failed))

    def bind(self, token, job_id):
        with self.database.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM analysis_batch_retry_authorizations WHERE authorization_id=? "
                "AND retry_job_id IS NOT NULL AND retry_job_id<>? LIMIT 1", (token, job_id),
            ).fetchone():
                raise AnalysisReservationConflict("重試授權已由其他工作使用")
            cursor = connection.execute(
                "UPDATE analysis_batch_retry_authorizations SET retry_job_id=? WHERE authorization_id=?",
                (job_id, token),
            )
            if not cursor.rowcount:
                raise AnalysisReservationConflict("找不到已記錄的失敗項目重試授權")
