"""Reuse shared content coverage; keep paid-lane control separate from free backfill."""
from inktime.app.repositories.analysis_backfill import AnalysisBackfillRepository
from inktime.app.repositories.analysis_reservations import AnalysisReservationConflict


class OpenAIBackfillRepository(AnalysisBackfillRepository):
    def control(self):
        with self.database.session() as connection:
            return dict(connection.execute("SELECT * FROM openai_backfill_control WHERE id=1").fetchone())

    def candidates(self, limit):
        with self.database.transaction() as connection:
            scan = connection.execute("SELECT COALESCE(MAX(completed_at),'') FROM scan_runs").fetchone()[0]
            control = self.control()
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

    def advance(self, connection, rows):
        last = rows[-1]
        connection.execute("UPDATE openai_backfill_control SET cursor_date=?,cursor_id=? WHERE id=1",
                           (last["captured_at"] or last["created_at"], last["id"]))

    def claim_hook(self, rows, provider_id):
        def claim(connection, job_id):
            accepted = {row[0] for row in connection.execute("SELECT photo_id FROM job_items WHERE job_id=?", (job_id,))}
            for row in rows:
                if row["id"] not in accepted:
                    continue  # Shared Batch may exclude local prefilter/cache hits.
                exists = connection.execute(
                    self._population_sql() + " SELECT 1 FROM population WHERE id=? AND sha256=? AND coverage_status='queued'",  # noqa: S608 -- fixed shared CTE, bound identity.
                    (row["id"], row["sha256"]),
                ).fetchone()
                if exists is None:
                    raise AnalysisReservationConflict("照片已完成或內容已改變，重新核對後再領取")
            self.advance(connection, rows)
        return claim

    def active_jobs(self):
        with self.database.session() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM jobs WHERE json_extract(settings_json,'$.complementary_lane')='openai_backfill' "
                "AND status IN ('pending','running','retrying','pausing','paused','budget_exceeded') ORDER BY created_at"
            )]

    def pause(self, kind, reason, cycle, job_id=None):
        with self.database.transaction() as connection:
            connection.execute("UPDATE openai_backfill_control SET pause_kind=?,pause_reason=?,pause_cycle=?,resume_job_id=? WHERE id=1",
                               (kind, reason, cycle, job_id))

    def clear_pause(self):
        with self.database.transaction() as connection:
            connection.execute("UPDATE openai_backfill_control SET pause_kind=NULL,pause_reason=NULL,pause_cycle=NULL,resume_job_id=NULL WHERE id=1")
