"""Content-based coverage read model and durable, bounded backfill ownership."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from inktime.app.repositories.analysis_reservations import AnalysisReservationConflict, reserved_analysis_sql
from inktime.app.repositories.model_coverage import remember_model_result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AnalysisBackfillRepository:
    def __init__(self, database):
        self.database = database

    def control(self) -> dict:
        with self.database.session() as connection:
            return dict(connection.execute("SELECT * FROM analysis_backfill_control WHERE id=1").fetchone())

    @staticmethod
    def uncertain_sql() -> str:
        return """EXISTS(SELECT 1 FROM billable_operations o WHERE o.content_sha256=p.sha256
                   AND (o.state IN ('started','response') OR EXISTS(
                     SELECT 1 FROM api_usage u WHERE u.operation_id=o.id AND u.cost_source='unknown')))
                OR EXISTS(SELECT 1 FROM analysis_batch_items bi JOIN analysis_batches b ON b.id=bi.batch_id
                   WHERE bi.content_sha256=p.sha256 AND (bi.status IN ('upload_unknown','submission_unknown')
                   OR b.status IN ('upload_unknown','submission_unknown')))"""

    def _population_sql(self, *, after: bool = False) -> str:
        cursor = "AND (COALESCE(p.captured_at,p.created_at),p.id)>(?,?)" if after else ""
        return f"""WITH population AS (
            SELECT p.id,p.sha256,p.relative_path,p.captured_at,p.created_at,
              COALESCE((SELECT t.job_id FROM analysis_backfill_tasks t WHERE t.content_sha256=p.sha256 ORDER BY t.id DESC LIMIT 1),
                (SELECT ji.job_id FROM job_items ji WHERE ji.photo_id=p.id ORDER BY ji.available_at DESC,ji.id DESC LIMIT 1)) AS last_job_id,
              CASE
                WHEN EXISTS(SELECT 1 FROM job_items ri JOIN jobs rj ON rj.id=ri.job_id
                  JOIN photos rp ON rp.id=ri.photo_id WHERE rp.sha256=p.sha256 AND ri.status='running'
                  AND rj.kind IN ('analysis','analysis_batch')) THEN 'running'
                WHEN ({self.uncertain_sql()}) THEN 'unknown'
                WHEN EXISTS(SELECT 1 FROM model_analysis_results c WHERE c.content_sha256=p.sha256) THEN 'completed'
                WHEN COALESCE(p.never_upload,0)=1 OR p.eligible=0 OR
                  p.exclusion_status IN ('auto_excluded','manually_excluded') OR
                  COALESCE(p.screenshot_likelihood,0)>=0.95 THEN 'excluded'
                WHEN {reserved_analysis_sql()} THEN 'queued'
                WHEN EXISTS(SELECT 1 FROM analysis_backfill_tasks t WHERE t.content_sha256=p.sha256
                  AND t.status IN ('failed','unknown')) OR EXISTS(
                  SELECT 1 FROM job_items fi JOIN jobs fj ON fj.id=fi.job_id
                  WHERE fi.photo_id=p.id AND fi.status='failed' AND fj.kind IN ('analysis','analysis_batch')) THEN 'failed'
                WHEN COALESCE(p.sha256,'')='' OR p.local_features_status<>'complete' THEN 'preprocessing'
                ELSE 'missing' END AS coverage_status
            FROM photos p JOIN libraries l ON l.id=p.library_id
            WHERE p.lifecycle_status='active' AND l.enabled=1 {cursor})
        """  # noqa: S608 -- fixed repository-owned SQL fragments.

    def status(self, *, category: str = "", offset: int = 0) -> dict:
        query = self._population_sql()
        with self.database.session() as connection:
            counts = {str(row["coverage_status"]): int(row["n"]) for row in connection.execute(
                query + " SELECT coverage_status,COUNT(*) n FROM population GROUP BY coverage_status"  # noqa: S608 -- repository-owned CTE.
            )}
            rows = connection.execute(
                query + " SELECT * FROM population WHERE (?='' OR coverage_status=?) "  # noqa: S608 -- repository-owned CTE; filter stays bound.
                "ORDER BY COALESCE(captured_at,created_at),id LIMIT 50 OFFSET ?",
                (category, category, max(0, offset)),
            ).fetchall()
            timing = connection.execute(
                "SELECT COUNT(*) n,MIN(i.started_at) first_at,MAX(i.completed_at) last_at "
                "FROM analysis_backfill_tasks t JOIN job_items i ON i.job_id=t.job_id AND i.photo_id=t.photo_id "
                "WHERE i.status='completed' AND i.stage='single' AND i.completed_at>=?",
                (datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00"),),
            ).fetchone()
            active_jobs = [dict(row) for row in connection.execute(
                "SELECT id,status,total_items,completed_items,failed_items FROM jobs "
                "WHERE json_extract(settings_json,'$.missing_analysis_backfill')=1 "
                "AND status IN ('pending','running','retrying','pausing','paused','budget_exceeded') ORDER BY created_at"
            )]
            failed_jobs = [dict(row) for row in connection.execute(
                "SELECT id,status,total_items,completed_items,failed_items FROM jobs "
                "WHERE json_extract(settings_json,'$.missing_analysis_backfill')=1 "
                "AND status IN ('completed_with_errors','failed') AND failed_items>0 ORDER BY created_at DESC LIMIT 20"
            )]
            scan = connection.execute(
                "SELECT completed_at FROM scan_runs WHERE completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
        values = {key: counts.get(key, 0) for key in
                  ("completed", "missing", "queued", "running", "failed", "unknown", "excluded", "preprocessing")}
        total = sum(values.values())
        rate = None
        if timing["n"] and timing["first_at"] and timing["last_at"]:
            elapsed = (datetime.fromisoformat(timing["last_at"]) - datetime.fromisoformat(timing["first_at"])).total_seconds()
            if elapsed > 0:
                rate = round(int(timing["n"]) * 60 / elapsed, 2)
        return {**values, "total": total, "unprocessed": total - values["completed"] - values["excluded"],
                "percent": round(100 * values["completed"] / total, 1) if total else 100.0,
                "photos_per_minute": rate,
                "eta_minutes": round(values["missing"] / rate, 1) if rate else None,
                "items": [dict(row) for row in rows], "active_jobs": active_jobs, "failed_jobs": failed_jobs,
                "last_scan_at": scan[0] if scan else None, "control": self.control()}

    def candidates(self, limit: int) -> list[dict]:
        control = self.control()
        if control["inventory_done"]:
            return []
        with self.database.session() as connection:
            rows = connection.execute(
                self._population_sql(after=True) + " SELECT * FROM population WHERE coverage_status='missing' "  # noqa: S608 -- fixed CTE and bound cursor.
                "ORDER BY COALESCE(captured_at,created_at),id LIMIT ?",
                (control["cursor_date"], control["cursor_id"], max(1, min(limit, 16))),
            ).fetchall()
        seen: set[str] = set()
        selected = []
        for row in rows:
            if row["sha256"] not in seen:
                seen.add(row["sha256"])
                selected.append(dict(row))
        return selected

    def claim_hook(self, rows: list[dict], provider_id: str):
        def claim(connection, job_id):
            control = connection.execute("SELECT * FROM analysis_backfill_control WHERE id=1").fetchone()
            if not control["enabled"] or control["pause_reason"] or control["provider_id"] != provider_id:
                raise AnalysisReservationConflict("補齊設定已暫停或變更，保留照片待處理")
            for row in rows:
                eligible = connection.execute(
                    self._population_sql() + " SELECT 1 FROM population WHERE id=? AND sha256=? "  # noqa: S608 -- fixed CTE and bound content identity.
                    # The new job already owns these items in this same transaction.
                    "AND coverage_status='queued'", (row["id"], row["sha256"]),
                ).fetchone()
                if eligible is None:
                    raise AnalysisReservationConflict("照片內容或模型資訊已變更，重新核對後再領取")
                connection.execute(
                    "INSERT INTO analysis_backfill_tasks(content_sha256,photo_id,job_id,status,created_at,updated_at) "
                    "VALUES (?,?,?,'queued',?,?)", (row["sha256"], row["id"], job_id, utc_now(), utc_now()),
                )
            last = rows[-1]
            connection.execute("UPDATE analysis_backfill_control SET cursor_date=?,cursor_id=? WHERE id=1",
                               (last["captured_at"] or last["created_at"], last["id"]))
        return claim

    def scan_completed(self) -> None:
        # A new older photograph must be discoverable even before the old cursor.
        with self.database.transaction() as connection:
            connection.execute("UPDATE analysis_backfill_control SET cursor_date='',cursor_id='',inventory_done=0 WHERE id=1")
        self.refresh()

    def refresh(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE analysis_backfill_tasks AS t SET status=CASE
                    WHEN EXISTS(SELECT 1 FROM job_items i WHERE i.job_id=t.job_id AND i.photo_id=t.photo_id AND i.status='running') THEN 'running'
                    WHEN EXISTS(SELECT 1 FROM model_analysis_results c WHERE c.content_sha256=t.content_sha256) THEN 'completed'
                    WHEN EXISTS(SELECT 1 FROM billable_operations o WHERE o.content_sha256=t.content_sha256 AND o.state IN ('started','response')) THEN 'unknown'
                    WHEN EXISTS(SELECT 1 FROM job_items i WHERE i.job_id=t.job_id AND i.photo_id=t.photo_id AND i.status IN ('failed','cancelled')) THEN 'failed'
                    WHEN EXISTS(SELECT 1 FROM job_items i WHERE i.job_id=t.job_id AND i.photo_id=t.photo_id AND i.status='completed') THEN 'completed'
                    ELSE 'queued' END,
                    error_code=(SELECT i.error_code FROM job_items i WHERE i.job_id=t.job_id AND i.photo_id=t.photo_id),updated_at=?
                    WHERE t.status IN ('queued','running')""", (utc_now(),),
            )

    def reconcile(self, *, month: str | None = None) -> dict:
        # Keep writer holds bounded so a 100k-photo audit can coexist with the
        # frame scheduler. The migration uses one atomic transaction separately.
        count = 0
        for source in ("live", "archive", "seasonal"):
            after = 0 if source != "archive" else ""
            while True:
                with self.database.session() as connection:
                    if source == "live":
                        rows = connection.execute(
                            "SELECT a.*,COALESCE(a.content_sha256,p.sha256) AS result_sha "
                            "FROM photo_analysis a JOIN photos p ON p.id=a.photo_id "
                            "WHERE a.score_kind='semantic' AND a.id>? ORDER BY a.id LIMIT 100", (after,),
                        ).fetchall()
                        snapshots = [(row["result_sha"], dict(row)) for row in rows]
                    else:
                        query = (
                            "SELECT id,content_sha256,analyses_json AS snapshots FROM photo_content_history WHERE id>? ORDER BY id LIMIT 100"
                            if source == "archive" else
                            "SELECT id,content_sha256,analysis_snapshot_json AS snapshots FROM seasonal_analysis_tasks "
                            "WHERE id>? AND analysis_snapshot_json IS NOT NULL ORDER BY id LIMIT 100"
                        )
                        rows = connection.execute(query, (after,)).fetchall()
                        snapshots = []
                        for row in rows:
                            try:
                                saved = json.loads(row["snapshots"])
                                for snapshot in saved if isinstance(saved, list) else [saved]:
                                    if isinstance(snapshot, dict):
                                        snapshots.append((row["content_sha256"], snapshot))
                            except (TypeError, ValueError):
                                continue
                if not rows:
                    break
                with self.database.transaction() as connection:
                    for sha, snapshot in snapshots:
                        count += int(remember_model_result(connection, str(sha or ""), snapshot))
                after = rows[-1]["id"]
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE analysis_backfill_control SET last_reconciled_at=?,"
                "last_monthly_audit=COALESCE(?,last_monthly_audit),cursor_date='',cursor_id='',inventory_done=0 WHERE id=1", (utc_now(), month),
            )
        self.refresh()
        return {"audited_results": count}

    def covered(self, sha: str) -> bool:
        with self.database.session() as connection:
            return connection.execute("SELECT 1 FROM model_analysis_results WHERE content_sha256=?", (sha,)).fetchone() is not None

    def pause(self, reason: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("UPDATE analysis_backfill_control SET pause_reason=?,updated_at=? WHERE id=1",
                               (reason[:500], utc_now()))

    def has_uncertain_content(self, sha: str) -> bool:
        with self.database.session() as connection:
            return connection.execute(
                f"SELECT 1 FROM photos p WHERE p.sha256=? AND ({self.uncertain_sql()}) LIMIT 1",  # noqa: S608
                (sha,),
            ).fetchone() is not None
