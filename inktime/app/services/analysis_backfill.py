"""Fill absent semantic information without changing the normal seasonal mode."""

from __future__ import annotations

from datetime import datetime
import json

from inktime.app.domain.analysis.execution_mode import execution_mode, permits_manual_ai
from inktime.app.domain.analysis.plan import canonical_json, stored_analysis_fingerprint
from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.repositories.analysis_reservations import AnalysisReservationConflict
from inktime.app.repositories.billable_operations import SeasonalAlreadyProcessedError, UnreconciledOperationError
from inktime.app.repositories.analysis_backfill import utc_now


class AnalysisBackfillService:
    PROVIDER_ID = "inktime-free-backfill"

    def __init__(self, repository, jobs, job_service, photos, analysis, settings, providers, scoring):
        self.repository, self.jobs, self.job_service = repository, jobs, job_service
        self.photos, self.analysis, self.settings = photos, analysis, settings
        self.providers, self.scoring = providers, scoring

    def profile(self) -> dict:
        provider = self.providers.repository.get(self.PROVIDER_ID)
        control = self.repository.control()
        configured: dict = next((row for row in self.providers.repository.list() if row["id"] == self.PROVIDER_ID), {})
        return {"model": str((provider or {}).get("model") or ""),
                "api_key_configured": bool(configured.get("api_key_configured")),
                "concurrency": control["concurrency"], "rpm": control["rpm"],
                "free_only": True, "enabled": bool(control["enabled"])}

    def configure(self, payload: dict, actor: str) -> None:
        model, api_key = payload.get("model"), payload.get("api_key", "")
        if not isinstance(model, str) or not model.strip() or not isinstance(api_key, str) or len(api_key) > 2048:
            raise ValueError("請填寫模型 ID；API Key 必須是字串")
        concurrency, rpm = payload.get("concurrency", 2), payload.get("rpm", 20)
        if type(concurrency) is not int or not 1 <= concurrency <= 8 or type(rpm) is not int or not 1 <= rpm <= 20:
            raise ValueError("並行數須為 1–8，RPM 須為 1–20")
        if self.repository.control()["enabled"]:
            raise ValueError("請先關閉補齊，再修改專用模型設定；已送出的請求會安全完成")
        with self.repository.database.session() as connection:
            active = connection.execute(
                "SELECT 1 FROM job_items i JOIN jobs j ON j.id=i.job_id "
                "WHERE json_extract(j.settings_json,'$.missing_analysis_backfill')=1 AND i.status='running' LIMIT 1"
            ).fetchone()
        if active:
            raise ValueError("仍有已領取的補齊請求，請等它完成後再修改設定")
        if not api_key and not self.profile()["api_key_configured"]:
            raise ValueError("請先設定補齊專用 OpenRouter API Key")
        self.providers.repository.save({
            "id": self.PROVIDER_ID, "name": "一鍵補齊專用 OpenRouter", "kind": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "model": model.strip(), "api_key": api_key,
            "enabled": True, "supports_vision": True, "supports_json_schema": True,
            "max_concurrency": concurrency, "rate_limit_rpm": rpm, "timeout_seconds": 120,
            "options": {"free_only": True, "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0}},
        }, actor, usage_scope="backfill")
        # Live catalog + max_price=0 still gate every request. This supplies the
        # zero-cost reserve estimate before entering the paid-operation boundary.
        self.providers.repository.save_pricing(self.PROVIDER_ID, {
            "model": model.strip(), "input_per_million": 0, "cached_input_per_million": 0,
            "output_per_million": 0, "batch_multiplier": 1,
        })
        with self.repository.database.transaction() as connection:
            connection.execute(
                "UPDATE analysis_backfill_control SET provider_id=?,concurrency=?,rpm=?,pause_reason=NULL,"
                "updated_by=?,updated_at=? WHERE id=1", (self.PROVIDER_ID, concurrency, rpm, actor, utc_now()),
            )

    def set_enabled(self, enabled: bool, actor: str) -> dict:
        if enabled:
            self._plan()
        with self.repository.database.transaction() as connection:
            connection.execute(
                "UPDATE analysis_backfill_control SET enabled=?,pause_reason=NULL,updated_by=?,updated_at=?,"
                "cursor_date='',cursor_id='',inventory_done=0 WHERE id=1",
                (int(enabled), actor, utc_now()),
            )
        if not enabled:
            self._pause_jobs()
        return {"enabled": enabled, "normal_mode": execution_mode(self.settings)}

    def _pause_jobs(self) -> None:
        for job in self._active_jobs():
            self.jobs.request_pause(str(job["id"]))

    def _active_jobs(self) -> list[dict]:
        with self.repository.database.session() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM jobs WHERE json_extract(settings_json,'$.missing_analysis_backfill')=1 "
                "AND status IN ('pending','running','retrying','pausing','paused','budget_exceeded') ORDER BY created_at"
            )]

    def _plan(self) -> dict:
        if not permits_manual_ai(execution_mode(self.settings)):
            raise ValueError("目前分析模式不允許使用模型")
        provider = self.providers.repository.get(self.PROVIDER_ID)
        if not provider or not provider["enabled"] or provider.get("usage_scope") != "backfill" or not self.profile()["api_key_configured"]:
            raise ValueError("請先儲存補齊專用 API Key 與模型")
        if not provider["options"].get("free_only"):
            raise ValueError("補齊專用設定必須啟用僅限免費")
        route = [{"provider_id": self.PROVIDER_ID, "display_name": provider["name"],
                  "priority": 100, "model": provider["model"], "usage_scope": "backfill",
                  "config_revision": self.providers.config_revision(provider)}]
        return self.analysis.build_plan(strategy="single", provider_route=route,
                                        scoring_profile_id=str(self.scoring.current()["id"]))

    def ensure(self) -> dict:
        control = self.repository.control()
        if not control["enabled"] or control["pause_reason"]:
            return {"job_id": None, "reason": control["pause_reason"] or "disabled"}
        lock = self.repository.database.try_acquire_operation_lock("missing-analysis-enqueue")
        if lock is None:
            return {"job_id": None, "reason": "queue_owned"}
        try:
            self.repository.refresh()
            plan = self._plan()
            active = self._active_jobs()
            if active:
                job = active[0]
                if job["status"] in {"pending", "paused"}:
                    # Only unclaimed items use the freshly configured route. Completed,
                    # failed and ambiguous items retain their original evidence.
                    with self.repository.database.transaction() as connection:
                        connection.execute(
                            "UPDATE jobs SET analysis_spec_json=?,analysis_fingerprint=?,settings_json=? "
                            "WHERE id=? AND status IN ('pending','paused') AND NOT EXISTS("
                            "SELECT 1 FROM job_items WHERE job_id=jobs.id AND status='running')",
                            (canonical_json(plan), stored_analysis_fingerprint(plan),
                             json.dumps({**json.loads(job["settings_json"]), "concurrency": control["concurrency"]}), job["id"]),
                        )
                    self.jobs.transition(str(job["id"]), {"pending", "paused"}, "running", "backfill_resumed")
                return {"job_id": job["id"], "reason": "existing_queue"}
            rows = self.repository.candidates(int(control["concurrency"]))
            if not rows:
                with self.repository.database.transaction() as connection:
                    connection.execute("UPDATE analysis_backfill_control SET inventory_done=1 WHERE id=1")
                return {"job_id": None, "reason": "no_missing_items"}
            job_id = self.jobs.create(
                name=f"補齊缺少模型資訊（{len(rows)} 張）", strategy="single", priority=6,
                settings={"missing_analysis_backfill": True, "concurrency": int(control["concurrency"]),
                          "force_ai": True, "max_attempts": 1, "source": "missing-analysis-backfill"},
                photo_ids=[row["id"] for row in rows], created_by=str(control["updated_by"] or "system"),
                analysis_spec=plan, analysis_fingerprint=stored_analysis_fingerprint(plan),
                commit_hook=self.repository.claim_hook(rows, self.PROVIDER_ID),
            )
            self.job_service.start(job_id)
            return {"job_id": job_id, "reason": "queued_missing"}
        except AnalysisReservationConflict:
            return {"job_id": None, "reason": "queue_owned"}
        except ValueError as exc:
            self.repository.pause(str(exc))
            self._pause_jobs()
            return {"job_id": None, "reason": str(exc)}
        finally:
            lock.close()

    def can_run(self, job_id: str) -> bool:
        control = self.repository.control()
        if not control["enabled"] or control["pause_reason"]:
            return False
        with self.repository.database.session() as connection:
            urgent = connection.execute(
                "SELECT 1 FROM jobs j JOIN job_items i ON i.job_id=j.id WHERE j.id<>? AND j.priority<6 "
                "AND j.kind<>'render' AND j.status IN ('running','retrying') AND i.status='pending' AND i.available_at<=? LIMIT 1",
                (job_id, utc_now()),
            ).fetchone()
        return urgent is None

    def retry_failed(self, source_job_id: str, actor: str) -> dict:
        source = self.jobs.get(source_job_id)
        if source is None or not json.loads(source["settings_json"]).get("missing_analysis_backfill"):
            raise ValueError("找不到原補齊工作")
        if source["status"] not in {"failed", "completed_with_errors"}:
            raise ValueError("請等原工作結束後再補救失敗項目")
        if not self.repository.control()["enabled"]:
            raise ValueError("請先開啟一鍵補齊，再重試失敗項目")
        plan = self._plan()
        plan_fingerprint = stored_analysis_fingerprint(plan)
        key = f"backfill-retry:{source_job_id}:{plan_fingerprint}"
        existing = self.jobs.get_by_dedupe_key(key)
        if existing:
            return {"job_id": existing["id"], "created": False}
        rows = []
        blocked = 0
        for photo_id in self.jobs.failed_photo_ids(source_job_id):
            photo = self.photos.get_with_path(photo_id)
            if photo is None or self.repository.covered(str(photo["sha256"] or "")):
                continue
            if self.repository.has_uncertain_content(str(photo["sha256"] or "")):
                blocked += 1
                continue
            rows.append({"id": photo_id, "sha256": photo["sha256"],
                         "captured_at": photo["captured_at"], "created_at": photo["created_at"]})
        if not rows:
            raise ValueError(f"沒有可安全重試的失敗項目；{blocked} 張回應／成本待確認，已有結果的照片已略過")
        control = self.repository.control()
        job_id = self.jobs.create(
            name="補齊失敗照片－改用目前專用模型", strategy="single", priority=6,
            settings={"missing_analysis_backfill": True, "concurrency": int(control["concurrency"]),
                      "force_ai": True, "max_attempts": 1, "parent_job_id": source_job_id},
            photo_ids=[row["id"] for row in rows], created_by=actor, dedupe_key=key,
            analysis_spec=plan, analysis_fingerprint=plan_fingerprint,
            commit_hook=self.repository.claim_hook(rows, self.PROVIDER_ID),
        )
        self.job_service.start(job_id)
        return {"job_id": job_id, "created": True, "affected": len(rows), "blocked": blocked}

    def enqueue_audit(self, now: datetime, *, after_scan: bool = False) -> str | None:
        month = now.strftime("%Y-%m")
        control = self.repository.control()
        if not after_scan and control["last_monthly_audit"] == month:
            return None
        key = f"model-coverage-audit:{'scan' if after_scan else month}"
        if self.jobs.has_active_dedupe(key):
            return None
        job_id = self.jobs.create_maintenance(
            kind="analysis_coverage_audit", name="模型資訊完整安全核對", priority=5,
            settings={"month": month, "max_attempts": 1}, created_by=None, dedupe_key=key,
        )
        self.job_service.start(job_id)
        return job_id

    def analyze_item(self, *, photo_id: str, job_id: str, **kwargs) -> dict:
        with self.repository.database.session() as connection:
            task = connection.execute(
                "SELECT * FROM analysis_backfill_tasks WHERE job_id=? AND photo_id=?", (job_id, photo_id),
            ).fetchone()
        if task is None:
            raise ValueError("補齊項目沒有持久化授權")
        photo = self.photos.get_with_path(photo_id)
        if photo is None or photo["sha256"] != task["content_sha256"]:
            raise ValueError("IMG-CHANGED 補齊照片內容已變更，請重新掃描")
        sha = str(task["content_sha256"])
        if self.repository.has_uncertain_content(sha):
            raise UnreconciledOperationError("先前請求的回應或成本不確定，請先確認原紀錄")
        if self.repository.covered(sha):
            inherited = self.photos.inherit_existing_analysis(photo_id, job_id, include_self=True, semantic_only=True)
            return {"analysis": inherited, "stage": "inherited", "_actual_cost": 0}
        try:
            result = self.analysis.analyze_photo(photo_id=photo_id, job_id=job_id,
                expected_content_sha256=sha, **kwargs)
        except SeasonalAlreadyProcessedError:
            inherited = self.photos.inherit_existing_analysis(photo_id, job_id, include_self=True, semantic_only=True)
            if inherited is None:
                raise UnreconciledOperationError("原模型資訊無法恢復，請確認原紀錄") from None
            return {"analysis": inherited, "stage": "inherited", "_actual_cost": 0}
        except Exception as exc:
            code = str(getattr(exc, "code", ""))
            if code == "BACKFILL-FREE-GUARD":
                self.repository.pause(str(exc))
                self._pause_jobs()
                raise ProviderHTTPError(str(exc), "VLM-005", retry_after=300,
                                        request_started=False, vision_started=False) from exc
            if getattr(exc, "not_sent", False) and not self.repository.control()["enabled"]:
                raise ProviderHTTPError("補齊已關閉，未送出項目保留佇列", "VLM-005", retry_after=60,
                                        request_started=False, vision_started=False) from exc
            raise
        if self.repository.has_uncertain_content(sha) or float(result.get("_actual_cost", 0) or 0) > 0:
            self.repository.pause("模型回報費用非零或成本尚待確認，請先查看 AI 追蹤與用量")
            self._pause_jobs()
        return result
