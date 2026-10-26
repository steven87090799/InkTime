"""Queue seasonal work without performing model calls in HTTP or scheduler code."""

from datetime import date, datetime, timedelta, timezone
import json
import secrets
from zoneinfo import ZoneInfo

from inktime.app.domain.analysis.execution_mode import execution_mode, permits_manual_ai
from inktime.app.domain.analysis.plan import stored_analysis_fingerprint
from inktime.app.repositories.analysis_reservations import AnalysisReservationConflict
from inktime.app.repositories.billable_operations import SeasonalAlreadyProcessedError
from inktime.app.repositories.seasonal_analysis import utc_now


class SeasonalAnalysisService:
    CHUNK_SIZE = 500

    def __init__(self, repository, jobs, job_service, photos, analysis, settings, providers, scoring):
        self.repository, self.jobs, self.job_service = repository, jobs, job_service
        self.photos, self.analysis, self.settings = photos, analysis, settings
        self.providers, self.scoring = providers, scoring

    def today(self) -> date:
        return datetime.now(ZoneInfo(str(self.settings.get("general.timezone", "Asia/Taipei")))).date()

    def window(self) -> int:
        return int(self.settings.get("analysis.seasonal_window_days", 5))

    def enabled(self) -> bool:
        return execution_mode(self.settings) == "seasonal_ai"

    def preparation_target(self, now: datetime, *, device_id: str | None = None) -> date | None:
        """Start tomorrow only after a real morning display, never on download."""
        zone = ZoneInfo(str(self.settings.get("general.timezone", "Asia/Taipei")))
        local = now.astimezone(zone)
        if local.hour < 8:
            return None
        if self.settings.get("analysis.require_display_ack", True):
            start = local.replace(hour=8, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
            end = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            clause = " AND json_extract(metadata_json,'$.device_id')=?" if device_id else ""
            params = [start, end.astimezone(timezone.utc).isoformat()]
            if device_id:
                params.append(device_id)
            with self.repository.database.session() as connection:
                row = connection.execute(
                    "SELECT 1 FROM display_history WHERE selection_method='device_queue_ack' "
                    "AND displayed_at>=? AND displayed_at<?" + clause + " LIMIT 1", params,
                ).fetchone()
            if row is None:
                return None
        return local.date() + timedelta(days=int(self.settings.get("analysis.prepare_ahead_days", 1)))

    def _plan(self) -> dict:
        if not permits_manual_ai(execution_mode(self.settings)):
            raise ValueError("目前分析執行模式不允許使用模型")
        route = self.providers.usable_route_snapshot()
        if not route:
            raise ValueError("尚未設定可用 Vision Provider，請前往模型與 API")
        return self.analysis.build_plan(strategy="single", provider_route=route,
                                        scoring_profile_id=str(self.scoring.current()["id"]))

    def _create(self, rows: list[dict], target: date, actor: str, *, retry_task=None,
                confirmation=None, force=False, plan=None) -> str:
        plan = plan or self._plan()
        job_id = self.jobs.create(
            name=f"{'手動重試' if retry_task else '全部重跑' if force else '歷年同期'}：{target.isoformat()}（{len(rows)} 張）",
            strategy="single", settings={"concurrency": 1, "max_attempts": 1, "force_ai": True,
                "seasonal_analysis": True, "seasonal_retry_task_id": retry_task,
                "target_date": target.isoformat(), "source": "seasonal"},
            photo_ids=[row["id"] for row in rows], created_by=actor, priority=1,
            analysis_spec=plan, analysis_fingerprint=stored_analysis_fingerprint(plan), force_recompute=force,
            dedupe_key=f"seasonal-rerun:{confirmation[0]}" if confirmation else None,
            commit_hook=self.repository.claim_hook(rows, target, retry_task=retry_task, confirmation=confirmation),
        )
        self.job_service.start(job_id)
        return job_id

    def ensure(self, target: date | None = None) -> dict:
        if not self.enabled():
            return {"waiting": False, "job_id": None}
        if target is None:
            target = self.preparation_target(datetime.now(timezone.utc))
            if target is None:
                return {"waiting": False, "job_id": None, "reason": "waiting_for_morning_display_ack"}
        self.repository.refresh()
        missing: list[dict] = []
        waiting = False
        reuse_remaining = self.CHUNK_SIZE
        for rows in self.repository.candidate_pages(target, self.window()):
            self.repository.adopt_history(rows, target)
            if reuse_remaining:
                reuse_remaining -= self._reuse_completed(rows, reuse_remaining)
            # History adoption cannot create an eligible new paid request:
            # those rows already have an analysis or an unresolved paid state.
            for row in rows:
                if (row["task_id"] is None and row["existing_analysis_id"] is None
                        and row["paid_state"] is None and not row["reserved"]):
                    waiting = True
                    if len(missing) < self.CHUNK_SIZE:
                        missing.append(row)
                waiting = waiting or bool(row["reserved"])
            # Restart after enqueue/start interruption without creating another
            # job; each query and the retained new-work list stay bounded.
            with self.repository.database.session() as connection:
                active = connection.execute(
                    """SELECT DISTINCT j.id,j.status FROM seasonal_analysis_tasks t JOIN jobs j ON j.id=t.job_id
                       WHERE t.content_sha256 IN (SELECT value FROM json_each(?)) AND t.status IN ('queued','running')""",
                    (json.dumps([row["sha256"] for row in rows]),),
                ).fetchall()
            waiting = waiting or bool(active)
            for job in active:
                if job["status"] == "pending":
                    self.job_service.start(str(job["id"]))
            if len(missing) == self.CHUNK_SIZE:
                break
        job_id = None
        if missing:
            try:
                job_id = self._create(missing, target, "system")
            except AnalysisReservationConflict:
                return {"waiting": True, "job_id": None}
        return {"waiting": waiting, "job_id": job_id}

    def _reuse_completed(self, rows: list[dict], limit: int) -> int:
        # Reuse a completed result for exact copies, giving each copy its own
        # record. No plan/model fingerprint is required for historical reuse.
        with self.repository.database.session() as connection:
            duplicates = connection.execute(
                f"""SELECT p.id,p.sha256 FROM photos p JOIN libraries l ON l.id=p.library_id
                   WHERE p.sha256 IN (SELECT value FROM json_each(?))
                   AND {self.repository.ELIGIBLE}
                   AND NOT EXISTS (SELECT 1 FROM photo_analysis a WHERE a.photo_id=p.id AND a.score_kind='semantic')
                   AND EXISTS (SELECT 1 FROM seasonal_analysis_tasks t WHERE t.content_sha256=p.sha256
                       AND t.id=(SELECT max(latest.id) FROM seasonal_analysis_tasks latest WHERE latest.content_sha256=p.sha256)
                       AND t.status='completed' AND json_extract(t.analysis_snapshot_json,'$.score_kind')='semantic')
                   ORDER BY p.id LIMIT ?""",  # noqa: S608 -- fixed repository eligibility predicate
                (json.dumps([row["sha256"] for row in rows if row["existing_analysis_id"] or row["task_id"]]), limit),
            ).fetchall()
        for duplicate in duplicates:
            try:
                self.photos.inherit_existing_analysis(str(duplicate["id"]), None, semantic_only=True)
            except (ValueError, TypeError):
                # Invalid historic metadata must not stop healthy candidates
                # or authorize another paid request. Keep it for manual review.
                with self.repository.database.transaction() as connection:
                    self.repository.notify_failure(connection,
                        key=f"analysis:reuse:{duplicate['id']}:{duplicate['sha256']}",
                        photo_id=duplicate["id"], job_id=None, error_code="SEASONAL-REUSE-FAILED", status="failed")
        return len(duplicates)

    def before_render(self, settings: dict, *, job_id: str | None = None) -> bool:
        if not self.enabled():
            return False
        if settings.get("photo_ids") and not settings.get("display_prepare") and not settings.get("offline_prepare"):
            return False
        target_value = (settings.get("offline_prepare") or {}).get("target_date") or (settings.get("history") or {}).get("history_date")
        target = date.fromisoformat(str(target_value)) if target_value else self.today()
        local_now = datetime.now(ZoneInfo(str(self.settings.get("general.timezone", "Asia/Taipei"))))
        if target > local_now.date() and self.preparation_target(local_now) is None:
            return True
        # Publish the best completed results by 07:30, while missing analyses
        # remain in their durable queue; never mark missing AI as successful.
        if target <= local_now.date() and (local_now.hour, local_now.minute) >= (7, 30):
            return False
        waiting = bool(self.ensure(target)["waiting"])
        if waiting and job_id is not None:
            self.repository.note_waiting(job_id, target)
        return waiting

    def analyze_item(self, *, photo_id: str, job_id: str, **kwargs) -> dict:
        with self.repository.database.session() as connection:
            task = connection.execute(
                "SELECT * FROM seasonal_analysis_tasks WHERE job_id=? AND photo_id=?", (job_id, photo_id)
            ).fetchone()
        if task is None or task["status"] not in {"queued", "running"}:
            raise ValueError("SEASONAL-OWNERSHIP 分析任務已結束或沒有送出授權")
        photo = self.photos.get_with_path(photo_id)
        if photo is None or photo["sha256"] != task["content_sha256"]:
            raise ValueError("IMG-CHANGED 同期任務的照片內容已變更，請重新掃描")
        with self.repository.database.transaction() as connection:
            connection.execute("UPDATE seasonal_analysis_tasks SET status='running',updated_at=? WHERE id=?", (utc_now(), task["id"]))
        try:
            if not kwargs.get("force_recompute", False):
                inherited = self.photos.inherit_existing_analysis(photo_id, job_id, include_self=True, semantic_only=True)
                if inherited is not None:
                    return {"analysis": inherited, "stage": "inherited", "_actual_cost": 0}
            result = self.analysis.analyze_photo(photo_id=photo_id, job_id=job_id,
                expected_content_sha256=str(task["content_sha256"]), **kwargs)
        except SeasonalAlreadyProcessedError:
            inherited = self.photos.inherit_existing_analysis(photo_id, job_id, include_self=True, semantic_only=True)
            if inherited is not None:
                return {"analysis": inherited, "stage": "inherited", "_actual_cost": 0}
            raise
        except Exception as exc:
            # Budget pauses happen before sending; keep the same durable owner.
            if str(getattr(exc, "code", "")).startswith("BUDGET-"):
                with self.repository.database.transaction() as connection:
                    connection.execute("UPDATE seasonal_analysis_tasks SET status='queued',updated_at=? WHERE id=?", (utc_now(), task["id"]))
            raise
        return result

    def retry(self, task_id: int, actor: str, *, accept_possible_charge: bool) -> str:
        self.repository.refresh()
        task = self.repository.task(task_id)
        if task is None or task["status"] not in {"failed", "unknown"}:
            raise ValueError("只有失敗或結果不明的照片可手動重試")
        if not accept_possible_charge:
            raise ValueError("請確認手動重試可能再次計費")
        rows = [row for row in self.repository.candidates(None, 0) if row["sha256"] == task["content_sha256"]]
        if not rows:
            raise ValueError("照片已變更、被排除或無法處理，請先檢查照片庫")
        original_job = self.jobs.get(str(task["job_id"])) if task["job_id"] else None
        force = bool(original_job and original_job["force_recompute"])
        return self._create(rows, self.today(), actor, retry_task=task_id, force=force)

    def rerun_preview(self, actor: str) -> dict:
        self.repository.refresh()
        rows = self.repository.candidates(None, 0)
        if not rows:
            raise ValueError("目前沒有可重新分析的照片")
        plan = self._plan()
        route = plan.get("provider_route") or []
        model = str((route[0].get("model") if route else None) or plan.get("model") or "")
        pricing = self.providers.repository.pricing(str(route[0]["provider_id"])).get(model) if route else None
        estimate = self.job_service.estimate(len(rows), "single", pricing=pricing)
        token = secrets.token_urlsafe(32)
        snapshot = self.repository.snapshot(rows)
        with self.repository.database.transaction() as connection:
            connection.execute(
                "INSERT INTO seasonal_rerun_confirmations(token,actor,stage,snapshot,plan_fingerprint,photo_count,expires_at) VALUES (?,?,1,?,?,?,?)",
                (token, actor, snapshot, stored_analysis_fingerprint(plan), len(rows), (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()),
            )
        return {"token": token, "stage": 1, "photo_count": len(rows), "estimate": estimate,
                "confirmation_phrase": f"重新分析 {len(rows)} 張照片"}

    def rerun_acknowledge(self, token: str, actor: str) -> None:
        with self.repository.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE seasonal_rerun_confirmations SET stage=2 WHERE token=? AND actor=? AND stage=1 AND expires_at>?",
                (token, actor, utc_now()),
            )
            if cursor.rowcount != 1:
                raise ValueError("確認已過期或步驟不正確，請重新預覽")

    def rerun(self, token: str, actor: str, phrase: str, accept_cost: bool) -> str:
        with self.repository.database.session() as connection:
            confirmation = connection.execute(
                "SELECT * FROM seasonal_rerun_confirmations WHERE token=? AND actor=? AND expires_at>?",
                (token, actor, utc_now()),
            ).fetchone()
        if confirmation is not None and confirmation["stage"] == 3:
            job = self.jobs.get(str(confirmation["job_id"]))
            if job is not None and job["status"] == "pending":
                self.job_service.start(str(job["id"]))
            return str(confirmation["job_id"])
        if confirmation is None or confirmation["stage"] != 2 or not accept_cost or phrase != f"重新分析 {confirmation['photo_count']} 張照片":
            raise ValueError("必須先預覽、確認額外費用，再輸入完整確認文字")
        rows = self.repository.candidates(None, 0)
        plan = self._plan()
        if stored_analysis_fingerprint(plan) != confirmation["plan_fingerprint"]:
            raise ValueError("模型或分析規格已變更，請重新預覽費用")
        return self._create(rows, self.today(), actor, force=True,
                            confirmation=(token, actor, str(confirmation["snapshot"])), plan=plan)
