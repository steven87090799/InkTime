"""Explicit daily-free and oldest-first paid lanes over the existing analysis contract."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from inktime.app.domain.analysis.execution_mode import execution_mode, permits_manual_ai
from inktime.app.domain.analysis.plan import stored_analysis_fingerprint
from inktime.app.domain.analysis.plan import analysis_output_token_cap
from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.providers.config import effective_provider_kind
from inktime.app.repositories.analysis_reservations import AnalysisReservationConflict
from inktime.app.repositories.billable_operations import SeasonalAlreadyProcessedError, UnreconciledOperationError
from inktime.app.services.openai_budget import OpenAIBudgetExceeded


class ComplementaryAnalysisService:
    def __init__(self, repository, jobs, job_service, photos, analysis, settings, providers, scoring, budgets, batch):
        self.repository, self.jobs, self.job_service = repository, jobs, job_service
        self.photos, self.analysis, self.settings = photos, analysis, settings
        self.providers, self.scoring, self.budgets, self.batch = providers, scoring, budgets, batch
        self.batch.complementary_pause_callback = self._pause_for_error

    def state(self):
        keys = ["analysis.complementary_enabled", "analysis.openai_provider_id", "analysis.daily_openrouter_provider_id",
                "analysis.openai_backfill_enabled", "analysis.openai_backfill_concurrency", "analysis.openai_backfill_chunk",
                "analysis.openai_backfill_mode", "analysis.openai_batch_consent", "openai_budget.cycle_limit",
                "openai_budget.daily_limit", "openai_budget.cycle_anchor", "openai_budget.cycle_months",
                "openai_budget.stop_percent", "openai_budget.warning_percent", "openai_budget.auto_resume"]
        providers = []
        for row in self.providers.repository.list():
            kind = effective_provider_kind(str(row.get("kind") or "openai_compatible"), str(row.get("base_url") or ""))
            if row.get("usage_scope", "normal") == "normal" and kind in {"openai", "openrouter"}:
                providers.append({**{key: row.get(key) for key in ("id", "name", "model", "enabled")}, "kind": kind})
        jobs = [{key: row[key] for key in ("id", "status", "total_items", "completed_items", "failed_items")}
                for row in self.repository.active_jobs()]
        tomorrow = datetime.now(ZoneInfo(str(self.settings.get("general.timezone", "Asia/Taipei")))).date() + timedelta(days=1)
        with self.repository.database.session() as connection:
            missing = connection.execute(
                "SELECT COUNT(*) FROM photos p JOIN libraries l ON l.id=p.library_id WHERE p.captured_month_day=? "
                "AND l.enabled=1 AND p.lifecycle_status='active' AND p.eligible=1 AND COALESCE(p.never_upload,0)=0 "
                "AND NOT EXISTS(SELECT 1 FROM model_analysis_results c WHERE c.content_sha256=p.sha256)",
                (tomorrow.strftime("%m-%d"),),
            ).fetchone()[0]
        return {"settings": self.settings.get_many(keys), "control": self.repository.control(), "providers": providers,
                "budget": self.budgets.openai.snapshot(), "jobs": jobs, "tomorrow": tomorrow.isoformat(), "tomorrow_missing": missing,
                "timezone": str(self.settings.get("general.timezone", "Asia/Taipei"))}

    def configure(self, updates, actor):
        allowed = set(self.state()["settings"])
        if not isinstance(updates, dict) or set(updates) - allowed:
            raise ValueError("分工設定包含不支援欄位")
        changed, _, merged = self.settings.prepare_updates(updates)
        if merged["analysis.complementary_enabled"]:
            self.providers.complementary_route("openai_backfill", str(merged["analysis.openai_provider_id"]))
            self.providers.complementary_route("openrouter_daily", str(merged["analysis.daily_openrouter_provider_id"]))
            if not permits_manual_ai(execution_mode(self.settings)):
                raise ValueError("目前分析模式不允許模型分析")
        with self.repository.database.transaction() as connection:
            stopping = updates == {"analysis.openai_backfill_enabled": False}
            if not stopping and connection.execute(
                "SELECT 1 FROM jobs j JOIN job_items i ON i.job_id=j.id WHERE i.status='running' "
                "AND json_extract(j.settings_json,'$.complementary_lane') IS NOT NULL LIMIT 1"
            ).fetchone():
                raise ValueError("請先暫停並等已送出的請求完成，再修改分工設定")
            self.settings.update_many_in_transaction(connection, changed, changed_by=actor, source_ip="complementary-settings")
            # Turning this off is an explicit manual pause, independent of renewal.
            if not merged["analysis.openai_backfill_enabled"] or not merged["analysis.complementary_enabled"]:
                connection.execute("UPDATE openai_backfill_control SET pause_kind='manual',pause_reason='使用者已暫停 OpenAI 補齊',resume_job_id=NULL WHERE id=1")
        self.settings.invalidate_runtime_cache()
        if not merged["analysis.openai_backfill_enabled"] or not merged["analysis.complementary_enabled"]:
            for job in self.repository.active_jobs():
                self.jobs.request_pause(str(job["id"]))
        elif "analysis.openai_backfill_enabled" in changed or "analysis.complementary_enabled" in changed:
            # Explicitly enabling is allowed to resume manually paused work.
            self.resume(actor)

    def resume(self, actor):
        if not self.settings.get("analysis.complementary_enabled", False):
            raise ValueError("請先儲存並啟用雙 Provider 分工")
        self.providers.complementary_route("openai_backfill")
        self.budgets.openai.assert_reserve()
        if not self.settings.get("analysis.openai_backfill_enabled", False):
            self.settings.update("analysis.openai_backfill_enabled", True, changed_by=actor, source_ip="complementary-resume")
        self.repository.clear_pause()
        for job in self.repository.active_jobs():
            if job["status"] in {"paused", "budget_exceeded"}:
                self.jobs.transition(str(job["id"]), {"paused", "budget_exceeded"}, "running", "openai_manual_resume")

    def _pause_for_error(self, exc, job_id=None):
        code = str(getattr(exc, "code", ""))
        kind = getattr(exc, "pause_kind", None)
        if code == "API_CREDIT_EXHAUSTED":
            kind = "credit"
        if not kind:
            kind = "review"
        state = self.budgets.openai.snapshot()
        marker = state["day_start"] if kind == "daily" else state["start"]
        self.repository.pause(kind, str(exc), marker, job_id)

    def _maybe_resume(self):
        control = self.repository.control()
        kind = control["pause_kind"]
        if not kind:
            return True
        if kind not in {"cycle", "daily", "credit"} or not self.settings.get("openai_budget.auto_resume", True):
            return False
        state = self.budgets.openai.snapshot()
        marker = state["day_start"] if kind == "daily" else state["start"]
        if marker == control["pause_cycle"]:
            return False
        self.budgets.openai.assert_reserve()
        job_id = control["resume_job_id"]
        with self.repository.database.transaction() as connection:
            if kind == "credit":
                if control["credit_attempt_cycle"] == state["start"]:
                    return False
                connection.execute("UPDATE openai_backfill_control SET credit_attempt_cycle=? WHERE id=1", (state["start"],))
            if job_id:
                job = self.jobs.get(str(job_id))
                if job and job["status"] in {"paused", "budget_exceeded"}:
                    if kind == "credit":
                        # Only one in-flight request until the first real photo verifies renewal.
                        connection.execute("UPDATE jobs SET settings_json=json_set(settings_json,'$.concurrency',1) WHERE id=?", (job_id,))
                    connection.execute("UPDATE jobs SET status='running',pause_requested_at=NULL,heartbeat_at=? WHERE id=? AND status IN ('paused','budget_exceeded')",
                                       (datetime.now(timezone.utc).isoformat(), job_id))
            connection.execute("UPDATE openai_backfill_control SET pause_kind=NULL,pause_reason=NULL,pause_cycle=NULL,resume_job_id=NULL WHERE id=1")
        if job_id:
            self.jobs.add_event(str(job_id), "openai_cycle_resume", "已進入自訂預算新週期，繼續未完成照片")
        return True

    def can_run(self, job_id):
        if not self.settings.get("analysis.complementary_enabled", False) or not self.settings.get("analysis.openai_backfill_enabled", False):
            return False
        if self.repository.control()["pause_kind"]:
            return False
        with self.repository.database.session() as connection:
            urgent = connection.execute(
                "SELECT 1 FROM jobs j JOIN job_items i ON i.job_id=j.id WHERE j.id<>? AND j.priority<6 "
                "AND j.kind<>'render' AND j.status IN ('running','retrying') AND i.status='pending' "
                "AND julianday(i.available_at)<=julianday(?) LIMIT 1", (job_id, datetime.now(timezone.utc).isoformat()),
            ).fetchone()
        return urgent is None

    def ensure(self):
        if not self.settings.get("analysis.complementary_enabled", False) or not self.settings.get("analysis.openai_backfill_enabled", False):
            return {"reason": "disabled", "job_id": None}
        lock = self.repository.database.try_acquire_operation_lock("openai-backfill-enqueue")
        if lock is None:
            return {"reason": "queue_owned", "job_id": None}
        try:
            if not self._maybe_resume():
                return {"reason": self.repository.control()["pause_kind"], "job_id": None}
            route = self.providers.complementary_route("openai_backfill")
            active = self.repository.active_jobs()
            if active:
                job = active[0]
                if job["status"] == "running" and self.jobs.finalize_if_done(str(job["id"])):
                    return {"reason": "queue_completed", "job_id": job["id"]}
                if job["status"] == "pending":
                    self.job_service.start(str(job["id"]))
                # A manually paused job never resumes just because a scheduler ran.
                return {"reason": "existing_queue", "job_id": job["id"]}
            if not self.can_run(""):
                return {"reason": "daily_work_first", "job_id": None}
            budget = self.budgets.openai.assert_reserve()
            count = int(self.settings.get("analysis.openai_backfill_chunk", 50))
            router = self.providers.build_router(route)
            try:
                per_photo = self.budgets.estimate_reserve(router.channels[0].provider, str(route[0]["model"]),
                    output_tokens=analysis_output_token_cap(self.settings),
                    batch=self.settings.get("analysis.openai_backfill_mode", "realtime") == "batch")
            finally:
                router.close()
            available = budget["remaining"]
            if budget["daily_limit"] > 0:
                available = min(available, max(0, budget["daily_stop_at"] - budget["daily_effective"]))
            if per_photo > 0:
                count = min(count, int(available / per_photo))
            if count < 1:
                raise OpenAIBudgetExceeded("daily" if budget["daily_limit"] > 0 and budget["daily_stop_at"] - budget["daily_effective"] < per_photo else "cycle",
                                          "OpenAI 剩餘預算不足以預留下一張照片；保留進度等待下期")
            rows = self.repository.candidates(count)
            if not rows:
                return {"reason": "no_missing_items", "job_id": None}
            settings = {"complementary_lane": "openai_backfill", "concurrency": int(self.settings.get("analysis.openai_backfill_concurrency", 1)),
                        "max_attempts": 1, "force_ai": True, "source": "openai-oldest-first"}
            if self.settings.get("analysis.openai_backfill_mode", "realtime") == "batch":
                result = self.batch.submit(scope="manual_selection", photo_ids=[row["id"] for row in rows], sample_count=len(rows),
                    created_by="system", privacy_acknowledged=self.settings.get("analysis.openai_batch_consent", False) is True,
                    _route=route, _job_settings=settings, _commit_hook=self.repository.claim_hook(rows, str(route[0]["provider_id"])))
                if not result["job_id"]:
                    with self.repository.database.transaction() as connection:
                        self.repository.advance(connection, rows)
                return {"reason": "batch_submitted", "job_id": result["job_id"]}
            plan = self.analysis.build_plan(strategy="single", provider_route=route, scoring_profile_id=str(self.scoring.current()["id"]))
            job_id = self.jobs.create(name=f"OpenAI 由舊到新補齊（{len(rows)} 張）", strategy="single", priority=6,
                settings=settings, photo_ids=[row["id"] for row in rows], created_by="system", analysis_spec=plan,
                analysis_fingerprint=stored_analysis_fingerprint(plan), commit_hook=self.repository.claim_hook(rows, str(route[0]["provider_id"])))
            self.job_service.start(job_id)
            return {"reason": "queued_oldest", "job_id": job_id}
        except AnalysisReservationConflict:
            return {"reason": "queue_owned", "job_id": None}
        except (ValueError, OpenAIBudgetExceeded) as exc:
            self._pause_for_error(exc)
            return {"reason": str(exc), "job_id": None}
        except Exception as exc:
            self._pause_for_error(exc, next((str(job["id"]) for job in self.repository.active_jobs()), None))
            return {"reason": "review_required", "job_id": None}
        finally:
            lock.close()

    def analyze_item(self, *, photo_id, job_id, **kwargs):
        photo = self.photos.get_with_path(photo_id)
        if photo is None:
            raise ValueError("照片不存在")
        sha = str(photo["sha256"])
        if self.repository.has_uncertain_content(sha):
            raise UnreconciledOperationError("先前請求回應或費用不確定，須先對帳")
        if self.repository.covered(sha):
            return {"analysis": self.photos.inherit_existing_analysis(photo_id, job_id, include_self=True, semantic_only=True),
                    "stage": "inherited", "_actual_cost": 0}
        if not self.can_run(job_id):
            raise ProviderHTTPError("每日工作優先或補齊已暫停；保留照片排隊", "VLM-005", retry_after=60,
                                    request_started=False, vision_started=False)
        try:
            return self.analysis.analyze_photo(photo_id=photo_id, job_id=job_id, expected_content_sha256=sha, **kwargs)
        except SeasonalAlreadyProcessedError:
            inherited = self.photos.inherit_existing_analysis(photo_id, job_id, include_self=True, semantic_only=True)
            if inherited is None:
                raise UnreconciledOperationError("原模型資訊尚待恢復，請確認原請求") from None
            return {"analysis": inherited, "stage": "inherited", "_actual_cost": 0}
        except Exception as exc:
            code = str(getattr(exc, "code", ""))
            if code.startswith("BUDGET-") or code in {"API_CREDIT_EXHAUSTED", "AUTH_REQUIRED", "CONFIG_INVALID", "VLM-008"}:
                self._pause_for_error(exc, job_id)
            raise
