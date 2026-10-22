"""Durable content ownership for demand-driven, one-image analysis.

Tasks are append-only attempts. Terminal failures remain excluded from automatic
selection, even after a model change, restart, rename, or overlapping window.
"""

from datetime import date, datetime, timezone
from contextlib import nullcontext
import hashlib
import json

from inktime.app.domain.analysis.seasonal import seasonal_month_days
from inktime.app.domain.photos.quality_policy import is_confirmed_screenshot
from inktime.app.repositories.analysis_history import historical_model_sql
from inktime.app.repositories.analysis_reservations import AnalysisReservationConflict, reserved_analysis_sql


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SeasonalAnalysisRepository:
    PAGE_SIZE = 500
    ELIGIBLE = """p.lifecycle_status='active' AND p.eligible=1 AND l.enabled=1
        AND p.exclusion_status NOT IN ('auto_excluded','manually_excluded')
        AND p.never_display=0
        AND COALESCE(p.never_upload,0)=0 AND p.local_features_status='complete'
        AND length(COALESCE(p.sha256,''))=64"""

    def __init__(self, database):
        self.database = database

    def candidates(self, target: date | None, window: int, *, connection=None) -> list[dict]:
        # Only the explicitly confirmed full-library rerun needs a materialized
        # snapshot. Daily demand and the progress page consume bounded pages.
        return [row for page in self.candidate_pages(target, window, connection=connection) for row in page]

    def candidate_pages(self, target: date | None, window: int, *, connection=None):
        days = seasonal_month_days(target, window) if target is not None else []
        after = ("", "")
        accepted_sha = None
        while True:
            context = self.database.session() if connection is None else nullcontext(connection)
            with context as active:
                rows = self._candidate_page(active, days, target is not None, after)
            if not rows:
                break
            page = []
            for row in rows:
                item = dict(row)
                if row["sha256"] != accepted_sha and not is_confirmed_screenshot(item):
                    page.append(item)
                    accepted_sha = row["sha256"]
            after = (rows[-1]["sha256"], rows[-1]["id"])
            if page:
                yield page
            if len(rows) < self.PAGE_SIZE:
                break

    def _candidate_page(self, connection, days, bounded_date, after):
        date_clause = "AND p.captured_month_day IN (SELECT value FROM json_each(?))" if bounded_date else ""
        date_params = (json.dumps(days),) if bounded_date else ()
        return connection.execute(
            f"""SELECT p.id,p.sha256,p.relative_path,p.exif_json,p.screenshot_likelihood,
                (SELECT t.id FROM seasonal_analysis_tasks t WHERE t.content_sha256=p.sha256
                 ORDER BY t.id DESC LIMIT 1) AS task_id,
                (SELECT a.id FROM photo_analysis a JOIN photos source ON source.id=a.photo_id
                 WHERE source.sha256=p.sha256 AND
                   ((a.score_kind='semantic' AND a.schema_version IN (4,5,6)) OR {historical_model_sql('a')})
                 ORDER BY a.id DESC LIMIT 1) AS existing_analysis_id,
                {reserved_analysis_sql()} AS reserved,
                (SELECT o.state FROM billable_operations o WHERE o.content_sha256=p.sha256
                 AND o.state NOT IN ('not_sent','approved') ORDER BY o.created_at DESC LIMIT 1) AS paid_state
                FROM photos p JOIN libraries l ON l.id=p.library_id
                WHERE {self.ELIGIBLE}
                  {date_clause}
                  AND (p.sha256,p.id)>(?,?)
                ORDER BY p.sha256,p.id LIMIT ?""",  # noqa: S608 -- fixed repository predicates only.
            (*date_params, *after, self.PAGE_SIZE),
        ).fetchall()

    @staticmethod
    def snapshot(rows: list[dict]) -> str:
        return hashlib.sha256(json.dumps(
            [(row["id"], row["sha256"]) for row in rows], separators=(",", ":")
        ).encode()).hexdigest()

    @staticmethod
    def notify_failure(connection, *, key: str, photo_id: str | None, job_id: str | None,
                       error_code: str, status: str) -> None:
        """Keep non-provider failures visible without duplicating a trace alert."""
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO device_notifications(
                kind,level,title,message,details_json,webhook_status,webhook_next_attempt_at,
                webhook_idempotency_key,created_at)
            SELECT 'analysis_failure','error','同期照片分析未完成',
                '請查看同期分析進度與原工作；結果不明的請求不會自動重送。',?,
                CASE WHEN (SELECT value_json FROM settings WHERE key='notification.webhook_enabled')='true'
                     AND COALESCE((SELECT json_extract(value_json,'$') FROM settings WHERE key='notification.webhook_url'),'')<>''
                     THEN 'pending' ELSE 'disabled' END,?,?,?
            WHERE ? IS NULL OR NOT EXISTS(
                SELECT 1 FROM device_notifications WHERE kind='analysis_failure'
                AND json_extract(details_json,'$.job_id')=?
                AND (json_extract(details_json,'$.photo_id')=? OR json_extract(details_json,'$.photo_id') IS NULL))""",
            (json.dumps({"photo_id": photo_id, "job_id": job_id, "error_code": error_code, "status": status}),
             now, key, now, job_id, job_id, photo_id),
        )

    def refresh(self) -> None:
        """Project queue outcomes; do not restart an unsuccessful attempt."""
        after_id = 0
        while True:
            with self.database.transaction() as connection:
                rows = connection.execute(
                    """SELECT t.*,i.status AS item_status,i.error_code AS item_error,j.status AS job_status
                       FROM seasonal_analysis_tasks t JOIN jobs j ON j.id=t.job_id
                       LEFT JOIN job_items i ON i.job_id=t.job_id AND i.photo_id=t.photo_id
                       WHERE t.status IN ('queued','running') AND t.id>? ORDER BY t.id LIMIT ?""",
                    (after_id, self.PAGE_SIZE),
                ).fetchall()
                if not rows:
                    return
                after_id = rows[-1]['id']
                for row in rows:
                    analysis = connection.execute(
                        "SELECT id FROM photo_analysis WHERE photo_id=? AND job_id=? "
                        "AND score_kind='semantic' ORDER BY id DESC LIMIT 1",
                        (row["photo_id"], row["job_id"]),
                    ).fetchone()
                    if analysis is None and row["item_error"] == "SEASONAL-ALREADY-PROCESSED":
                        recovered = connection.execute(
                            "SELECT a.* FROM photo_analysis a JOIN photos p ON p.id=a.photo_id "
                            "WHERE p.sha256=? AND a.score_kind='semantic' ORDER BY a.id DESC LIMIT 1",
                            (row["content_sha256"],),
                        ).fetchone()
                        if recovered is not None:
                            analysis = recovered
                            connection.execute(
                                "UPDATE seasonal_analysis_tasks SET analysis_snapshot_json=? WHERE id=?",
                                (json.dumps(dict(recovered), ensure_ascii=False), row["id"]),
                            )
                    status, error = str(row["status"]), row["item_error"]
                    if row["analysis_snapshot_json"] or (analysis is not None and row["item_error"] == "SEASONAL-ALREADY-PROCESSED"):
                        # Saving the model result and its snapshot was atomic. A
                        # crash after that commit must not invite a second charge.
                        status, error = "completed", None
                    elif row["item_status"] == "completed":
                        status = "completed" if analysis else "failed"
                        error = None if analysis else "SEASONAL-NO-MODEL-RESULT"
                    elif row["item_status"] == "failed" or row["job_status"] in {"cancelled", "failed", "completed_with_errors", "completed"}:
                        unknown = connection.execute(
                            "SELECT 1 FROM billable_operations WHERE content_sha256=? AND state='started' LIMIT 1",
                            (row["content_sha256"],),
                        ).fetchone()
                        status = "unknown" if unknown else "failed"
                        error = error or ("VLM-UNRECONCILED" if unknown else "SEASONAL-STOPPED")
                    elif row["item_status"] == "running":
                        status = "running"
                    connection.execute(
                        "UPDATE seasonal_analysis_tasks SET status=?,analysis_id=COALESCE(?,analysis_id),error_code=?,updated_at=? WHERE id=?",
                        (status, analysis["id"] if analysis else None, error, utc_now(), row["id"]),
                    )
                    if status in {"failed", "unknown"}:
                        self.notify_failure(connection, key=f"analysis:seasonal:{row['id']}",
                            photo_id=row["photo_id"], job_id=row["job_id"], error_code=error, status=status)

    def adopt_history(self, rows: list[dict], target: date) -> None:
        with self.database.transaction() as connection:
            for row in rows:
                if row["task_id"] is not None or row["reserved"]:
                    continue
                analysis_id, paid = row["existing_analysis_id"], row["paid_state"]
                if analysis_id is None and paid is None:
                    continue
                if connection.execute(
                    "SELECT 1 FROM seasonal_analysis_tasks WHERE content_sha256=? LIMIT 1", (row["sha256"],)
                ).fetchone():
                    continue
                status = "completed" if analysis_id is not None else "unknown" if paid == "started" else "failed"
                analysis = connection.execute("SELECT * FROM photo_analysis WHERE id=?", (analysis_id,)).fetchone() if analysis_id else None
                inserted = connection.execute(
                    """INSERT INTO seasonal_analysis_tasks(content_sha256,photo_id,analysis_id,analysis_snapshot_json,status,error_code,
                        target_date,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (row["sha256"], row["id"], analysis_id, json.dumps(dict(analysis), ensure_ascii=False) if analysis else None, status,
                     None if analysis_id else "SEASONAL-PAID-HISTORY", target.isoformat(), utc_now(), utc_now()),
                )
                if status in {"failed", "unknown"}:
                    self.notify_failure(connection, key=f"analysis:seasonal:{inserted.lastrowid}",
                        photo_id=row["id"], job_id=None, error_code="SEASONAL-PAID-HISTORY", status=status)

    def claim_hook(self, rows: list[dict], target: date, *, retry_task: int | None = None,
                   confirmation: tuple[str, str, str] | None = None):
        def commit(connection, job_id):
            if confirmation is not None:
                token, actor, snapshot = confirmation
                confirmed = connection.execute(
                    "SELECT * FROM seasonal_rerun_confirmations WHERE token=? AND actor=? AND stage=2 AND expires_at>?",
                    (token, actor, utc_now()),
                ).fetchone()
                current = self.candidates(None, 0, connection=connection)
                if confirmed is None or confirmed["snapshot"] != snapshot or self.snapshot(current) != snapshot:
                    raise ValueError("全庫範圍已改變或確認已過期，請重新預覽")
                connection.execute("UPDATE seasonal_rerun_confirmations SET stage=3,job_id=? WHERE token=?", (job_id, token))
            for row in rows:
                current = connection.execute("SELECT sha256 FROM photos WHERE id=?", (row["id"],)).fetchone()
                if current is None or current["sha256"] != row["sha256"]:
                    raise AnalysisReservationConflict("照片內容已變更，請重新取得同期範圍")
                prior = connection.execute(
                    "SELECT * FROM seasonal_analysis_tasks WHERE content_sha256=? ORDER BY id DESC LIMIT 1", (row["sha256"],)
                ).fetchone()
                if prior is not None and (prior["status"] in {"queued", "running"} or
                    (retry_task is None and confirmation is None) or
                    (retry_task is not None and prior["id"] != retry_task)):
                    raise AnalysisReservationConflict("相同照片已存在分析紀錄，拒絕重複送出")
                if prior is not None and prior["status"] == "unknown" and retry_task is None:
                    raise AnalysisReservationConflict("有待對帳照片；請先個別確認是否重送")
                if confirmation is None and retry_task is None and connection.execute(
                    "SELECT 1 FROM billable_operations WHERE content_sha256=? AND state NOT IN ('not_sent','approved') LIMIT 1",
                    (row["sha256"],),
                ).fetchone():
                    raise AnalysisReservationConflict("照片已有付費操作，請重新核對既有結果")
                if confirmation is not None and connection.execute(
                    "SELECT 1 FROM billable_operations WHERE content_sha256=? AND state IN ('started','response') LIMIT 1",
                    (row["sha256"],),
                ).fetchone():
                    raise AnalysisReservationConflict("有尚未對帳或待匯入的模型回應，請先處理原請求")
                if retry_task is not None:
                    if connection.execute(
                        "SELECT 1 FROM billable_operations o JOIN ai_cache_reservations r ON r.cache_key=o.request_fingerprint "
                        "WHERE o.content_sha256=? AND o.state='started' AND r.status='reserved' AND r.lease_until>? LIMIT 1",
                        (row["sha256"], utc_now()),
                    ).fetchone():
                        raise AnalysisReservationConflict("原請求仍有有效租約，請等待結束後再重試")
                    connection.execute(
                        "UPDATE billable_operations SET state='approved',updated_at=?,"
                        "resolution_note=COALESCE(resolution_note || char(10),'') || ? "
                        "WHERE content_sha256=? AND state='started'",
                        (utc_now(), f"管理員手動重試同期任務 {retry_task}；新工作 {job_id}，已接受可能重複費用", row["sha256"]),
                    )
                connection.execute(
                    """INSERT INTO seasonal_analysis_tasks(content_sha256,photo_id,job_id,status,target_date,created_at,updated_at)
                       VALUES (?,?,?,'queued',?,?,?)""",
                    (row["sha256"], row["id"], job_id, target.isoformat(), utc_now(), utc_now()),
                )
        return commit

    def task(self, task_id: int) -> dict | None:
        with self.database.session() as connection:
            row = connection.execute("SELECT * FROM seasonal_analysis_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def note_waiting(self, job_id: str, target: date) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO job_events(job_id,event,message,details_json,created_at)
                   SELECT ?,'seasonal_waiting','等待同期照片分析結束，再依既有資料選片',?,?
                   WHERE NOT EXISTS (SELECT 1 FROM job_events WHERE job_id=? AND event='seasonal_waiting')""",
                (job_id, json.dumps({"target_date": target.isoformat(), "progress_url": "/analysis/seasonal"}), utc_now(), job_id),
            )

    def status(self, target: date, window: int, *, limit: int = 100, offset: int = 0) -> dict:
        self.refresh()
        candidate_count = 0
        counts = {key: 0 for key in ("unprocessed", "queued", "running", "completed", "failed", "unknown", "reserved")}
        with self.database.session() as connection:
            for page in self.candidate_pages(target, window, connection=connection):
                candidate_count += len(page)
                for row in page:
                    task = connection.execute("SELECT status FROM seasonal_analysis_tasks WHERE id=?", (row["task_id"],)).fetchone()
                    state = task["status"] if task else "reserved" if row["reserved"] else "completed" if row["existing_analysis_id"] else "unknown" if row["paid_state"] else "unprocessed"
                    counts[state] += 1
            tasks = connection.execute(
                """SELECT t.*,p.relative_path,a.provider,a.model,a.created_at AS analyzed_at,
                      t.id=(SELECT max(latest.id) FROM seasonal_analysis_tasks latest WHERE latest.content_sha256=t.content_sha256) AS is_latest
                   FROM seasonal_analysis_tasks t LEFT JOIN photos p ON p.id=t.photo_id
                   LEFT JOIN photo_analysis a ON a.id=t.analysis_id
                   ORDER BY t.id DESC LIMIT ? OFFSET ?""", (limit, offset)
            ).fetchall()
            total = connection.execute("SELECT count(*) FROM seasonal_analysis_tasks").fetchone()[0]
            missing_dates = connection.execute(
                f"SELECT count(*) FROM photos p JOIN libraries l ON l.id=p.library_id WHERE {self.ELIGIBLE} "  # noqa: S608 -- fixed repository predicate.
                "AND (p.captured_month_day IS NULL OR p.capture_date_status<>'valid')"
            ).fetchone()[0]
        public_tasks = []
        for row in tasks:
            task = dict(row)
            snapshot = json.loads(task.pop("analysis_snapshot_json") or "{}")
            task["provider"] = task["provider"] or snapshot.get("provider")
            task["model"] = task["model"] or snapshot.get("model")
            public_tasks.append(task)
        return {"target_date": target.isoformat(), "window_days": window, "candidate_count": candidate_count,
                "counts": counts, "tasks": public_tasks, "total_attempts": total,
                "missing_capture_dates": missing_dates, "offset": offset}
