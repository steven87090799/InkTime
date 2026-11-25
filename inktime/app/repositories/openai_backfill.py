"""Reuse shared content coverage; keep paid-lane control separate from free backfill."""
import json
from datetime import date

from inktime.app.domain.analysis.seasonal import upcoming_display_priorities

from inktime.app.repositories.analysis_backfill import AnalysisBackfillRepository
from inktime.app.repositories.analysis_reservations import AnalysisReservationConflict


class OpenAIBackfillRepository(AnalysisBackfillRepository):
    def control(self):
        with self.database.session() as connection:
            return dict(connection.execute("SELECT * FROM openai_backfill_control WHERE id=1").fetchone())

    def candidates(self, limit, *, display_from: date | None = None, nearby_days: int = 0, ranked_only: bool = False):
        with self.database.transaction(immediate=display_from is None) as connection:
            scan = connection.execute("SELECT COALESCE(MAX(completed_at),'') FROM scan_runs").fetchone()[0]
            if display_from is not None:
                return self._display_candidates(connection, limit, display_from, scan, nearby_days, ranked_only)
            control = dict(connection.execute("SELECT * FROM openai_backfill_control WHERE id=1").fetchone())
            if scan != control["last_scan_at"]:
                connection.execute("UPDATE openai_backfill_control SET cursor_date='',cursor_id='',last_scan_at=? WHERE id=1", (scan,))
                control.update(cursor_date="", cursor_id="", last_scan_at=scan)
            rows = connection.execute(
                self._population_sql(after=True) + " SELECT * FROM population WHERE coverage_status='missing' "  # noqa: S608 -- fixed shared CTE, bound keyset and limit.
                "ORDER BY COALESCE(captured_at,created_at),id LIMIT ?",
                (control["cursor_date"], control["cursor_id"], max(1, min(500, limit))),
            ).fetchall()
        seen = set()
        selected = []
        for row in rows:
            if row["sha256"] not in seen:
                seen.add(row["sha256"])
                selected.append(dict(row))
        return selected

    def _display_candidates(self, connection, limit, today, scan, nearby_days, ranked_only):
        priorities = {} if ranked_only else upcoming_display_priorities(today, nearby_days)
        epoch = [today.isoformat(), scan, nearby_days, ranked_only]
        saved = json.loads(connection.execute(
            "SELECT monthly_cursor FROM openai_backfill_control WHERE id=1").fetchone()[0] or "{}")
        first = [-1, 0, 0, "", ""]
        after = saved["after"] if saved.get("epoch") == epoch else first
        rows = connection.execute(
            self._population_sql(display_priority=True) + """, priorities AS MATERIALIZED (
                SELECT key AS month_day,value AS priority FROM json_each(?)), ranked AS (
                SELECT p.*,CASE WHEN p.never_display=1 THEN 101 ELSE COALESCE(d.priority,100) END AS day_rank,
                  COALESCE(p.display_count,0) AS exposures,-COALESCE(p.local_candidate_score,0) AS quality_order,
                  COALESCE(p.captured_at,p.created_at) AS capture_order
                FROM population p LEFT JOIN priorities d ON d.month_day=p.captured_month_day
                WHERE NOT EXISTS(SELECT 1 FROM model_analysis_results c WHERE c.content_sha256=p.sha256)
                AND p.coverage_status='missing')
            SELECT * FROM ranked WHERE (day_rank,exposures,quality_order,capture_order,id)>(?,?,?,?,?)
            ORDER BY day_rank,exposures,quality_order,capture_order,id LIMIT ?""",  # noqa: S608 -- fixed CTE and bound cursor.
            (json.dumps(priorities), *after, max(1, min(500, limit))),
        ).fetchall()
        if not rows:
            return []
        selected = {}
        for row in rows:
            if row["sha256"] not in selected:
                item = dict(row)
                cursor = [row[k] for k in ("day_rank", "exposures", "quality_order", "capture_order", "id")]
                item["monthly_cursor"] = json.dumps({"epoch": epoch, "after": cursor})
                selected[row["sha256"]] = item
        return list(selected.values())

    def advance(self, connection, rows):
        last = rows[-1]
        if "monthly_cursor" in last:
            connection.execute("UPDATE openai_backfill_control SET monthly_cursor=? WHERE id=1", (last["monthly_cursor"],))
            return
        connection.execute("UPDATE openai_backfill_control SET cursor_date=?,cursor_id=? WHERE id=1",
                           (last["captured_at"] or last["created_at"], last["id"]))

    def claim_hook(self, rows, provider_id, *, policy="oldest"):
        def claim(connection, job_id):
            if not self.enabled(connection):
                raise AnalysisReservationConflict("OpenAI 補齊已暫停，重新領取未送出")
            cfg = {row[0]: json.loads(row[1]) for row in connection.execute(
                "SELECT key,value_json FROM settings WHERE key IN "
                "('analysis.openai_backfill_policy','analysis.openai_provider_id','analysis.openai_secondary_provider_id')")}
            accounts = [cfg.get("analysis.openai_provider_id")]
            if policy == "monthly_split":
                accounts.append(cfg.get("analysis.openai_secondary_provider_id"))
            if policy != cfg.get("analysis.openai_backfill_policy", "oldest") or provider_id not in accounts:
                raise AnalysisReservationConflict("OpenAI 補齊設定已變更，重新選取帳戶與照片")
            accepted = {row[0] for row in connection.execute("SELECT photo_id FROM job_items WHERE job_id=?", (job_id,))}
            # Shared Batch can omit prefilter/cache hits. Recheck only the
            # accepted photos together, inside the bounded base-table scope.
            requested = {(row["id"], row["sha256"]) for row in rows if row["id"] in accepted}
            if requested:
                eligible = connection.execute(
                    self._population_sql(selected=True) +  # noqa: S608 -- fixed shared CTE, bound identities.
                    " SELECT id,sha256 FROM population WHERE coverage_status='queued'",  # noqa: S608
                    (json.dumps(sorted(identifier for identifier, _sha in requested)),),
                ).fetchall()
                if {(row["id"], row["sha256"]) for row in eligible} != requested:
                    raise AnalysisReservationConflict("照片已完成或內容已改變，重新核對後再領取")
            self.advance(connection, rows)
        return claim

    @staticmethod
    def enabled(connection):
        return connection.execute(
            "SELECT COUNT(*) FROM settings WHERE key IN "
            "('analysis.complementary_enabled','analysis.openai_backfill_enabled') AND value_json='true'"
        ).fetchone()[0] == 2 and connection.execute(
            "SELECT 1 FROM openai_backfill_control WHERE id=1 AND pause_kind IS NULL "
            "AND NOT EXISTS(SELECT 1 FROM settings WHERE key='analysis.execution_mode' "
            "AND json_extract(value_json,'$') IN ('disabled','local_only'))"
        ).fetchone() is not None

    def active_jobs(self):
        with self.database.session() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM jobs WHERE json_extract(settings_json,'$.complementary_lane')='openai_backfill' "
                "AND status IN ('pending','running','retrying','pausing','paused','budget_exceeded') ORDER BY created_at"
            )]

    def hold_account(self, job_id, provider_id, cycle, *, required=None):
        with self.database.transaction() as connection:
            if required is None:
                connection.execute("INSERT INTO openai_account_holds(provider_id,cycle_start) VALUES (?,?) "
                                   "ON CONFLICT(provider_id) DO UPDATE SET cycle_start=excluded.cycle_start", (provider_id, cycle))
            connection.execute("UPDATE jobs SET settings_json=json_set(settings_json,'$.openai_account_pause',?,"
                               "'$.openai_required_amount',?) WHERE id=?", (cycle, required or 0, job_id))

    def release_unpaid_items(self, job_id):
        """Let another funded account claim only definitively unexecuted realtime items."""
        with self.database.transaction() as connection:
            released = connection.execute(
                """UPDATE job_items AS i SET status='cancelled',error_code='OPENAI-ACCOUNT-HANDOFF',
                   completed_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
                   WHERE i.job_id=? AND i.status='pending' AND EXISTS(
                     SELECT 1 FROM jobs j WHERE j.id=i.job_id AND j.kind='analysis'
                     AND j.status IN ('paused','budget_exceeded'))
                   AND i.analysis_content_sha256 IS NOT NULL AND NOT EXISTS(
                     SELECT 1 FROM billable_operations o WHERE o.content_sha256=i.analysis_content_sha256
                     AND o.state<>'not_sent')""", (job_id,)).rowcount
            if released:
                connection.execute("UPDATE openai_backfill_control SET monthly_cursor=NULL WHERE id=1")
                connection.execute("UPDATE jobs SET status='cancelled',completed_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now') "
                                   "WHERE id=? AND NOT EXISTS(SELECT 1 FROM job_items WHERE job_id=? AND status IN "
                                   "('pending','running','retrying'))", (job_id, job_id))
                connection.execute("INSERT INTO job_events(job_id,event,message,details_json,created_at) "
                                   "VALUES (?,'account_handoff','未執行的照片已交回每月補齊佇列','{}',"
                                   "strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))", (job_id,))
        return released

    def pause(self, kind, reason, cycle, job_id=None):
        with self.database.transaction() as connection:
            connection.execute("UPDATE openai_backfill_control SET pause_kind=?,pause_reason=?,pause_cycle=?,"
                               "resume_job_id=COALESCE(?,resume_job_id) WHERE id=1 AND pause_kind IS NOT 'manual' "
                               "AND (pause_kind IS NOT 'review' OR ?='review')", (kind, reason, cycle, job_id, kind))

    def clear_pause(self):
        with self.database.transaction() as connection:
            connection.execute("UPDATE openai_backfill_control SET pause_kind=NULL,pause_reason=NULL,pause_cycle=NULL,resume_job_id=NULL WHERE id=1")
