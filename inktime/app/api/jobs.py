from __future__ import annotations

import json
import math

from flask import Blueprint, Response, abort, current_app, g, render_template, request, stream_with_context

from inktime.app.core.json_values import (
    JsonScalarError,
    json_bool,
    json_int,
    json_object_payload,
    nullable_json_float,
    optional_json_int,
)
from inktime.app.core.idempotency import request_fingerprint, scoped_idempotency_key
from inktime.app.domain.analysis.plan import canonical_json, fingerprint, normalize_analysis_strategy
from inktime.app.domain.analysis.execution_mode import execution_mode, permits_automatic_ai
from inktime.app.web.ai_readiness import ai_readiness_snapshot
from inktime.app.services.jobs import InvalidJobTransition, JobService
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("jobs", __name__)
JOB_ITEM_PAGE_SIZE = 100


JOB_STATUS_LABELS = {
    "pending": "等待啟動",
    "preparing": "準備中",
    "running": "執行中",
    "pausing": "正在暫停",
    "paused": "已暫停",
    "retrying": "正在重試",
    "completed": "已完成",
    "completed_with_errors": "已完成，但有失敗項目",
    "failed": "執行失敗",
    "cancelled": "已取消",
    "budget_exceeded": "因成本安全規則暫停",
}


def _job_item_view(item) -> dict:
    status = str(item["status"])
    stage = str(item["stage"])
    attempts = int(item["attempts"] or 0)
    code = str(item["error_code"] or "")
    if stage == "no_content":
        return {
            "explanation_title": "目前沒有可顯示照片",
            "explanation_detail": "加入照片後，下次排程會再檢查。",
            "explanation_tone": "neutral",
        }
    if status == "completed" and stage == "local_fallback":
        return {
            "explanation_title": "本機備援完成（未呼叫模型）",
            "explanation_detail": (
                "這張照片沒有進入 Vision 呼叫；當時的 AI 選片模式或每日／每月上限只允許部分照片，"
                "所以工作改存本機品質結果。這不等於模型分析成功。"
            ),
            "explanation_tone": "warning",
        }
    if status == "completed" and stage == "prefilter":
        return {
            "explanation_title": "本機預篩選完成（未呼叫模型）",
            "explanation_detail": "照片在送出前已被本機規則排除，因此沒有傳給 Vision 模型。",
            "explanation_tone": "neutral",
        }
    if status == "completed" and stage == "local":
        return {
            "explanation_title": "本機分析完成（未呼叫模型）",
            "explanation_detail": "這是一筆只使用本機影像特徵的結果，沒有 Provider Token 或模型文案。",
            "explanation_tone": "neutral",
        }
    if status == "completed" and stage == "cache":
        return {
            "explanation_title": "已沿用模型快取",
            "explanation_detail": "相同照片與分析設定已有模型結果；本次未重複呼叫 Provider。",
            "explanation_tone": "success",
        }
    if status == "completed" and stage == "inherited":
        return {
            "explanation_title": "已沿用相同照片的模型結果",
            "explanation_detail": "系統找到相同影像與分析設定的既有結果；本次未重複呼叫 Provider。",
            "explanation_tone": "success",
        }
    if status == "completed":
        return {
            "explanation_title": "已成功",
            "explanation_detail": (
                f"過程中曾重試 {attempts - 1} 次；最後一次已成功，舊錯誤不影響結果。"
                if attempts > 1
                else "模型結果已成功儲存。"
            ),
            "explanation_tone": "success",
        }
    if status == "running":
        return {
            "explanation_title": "正在處理",
            "explanation_detail": "Worker 已領取此照片，正在執行目前階段。",
            "explanation_tone": "active",
        }
    if status == "pending" and not code:
        return {
            "explanation_title": "等待 Worker",
            "explanation_detail": "此照片仍在 Queue，尚未送進模型。",
            "explanation_tone": "neutral",
        }
    if status == "cancelled":
        return {
            "explanation_title": "已取消",
            "explanation_detail": "此照片不會再送進模型。",
            "explanation_tone": "neutral",
        }

    provider_message = ""
    provider_response_text = str(item["latest_provider_response"] or "")
    try:
        provider_payload = json.loads(provider_response_text)
        provider_error = provider_payload.get("error") if isinstance(provider_payload, dict) else None
        if isinstance(provider_error, dict):
            provider_message = str(provider_error.get("message") or "").strip()
    except (json.JSONDecodeError, TypeError, ValueError):
        provider_message = ""
    http_status = int(item["latest_http_status"] or 0)
    no_compatible_openrouter_endpoint = (
        http_status == 404
        and "no endpoints found that can handle the requested parameters"
        in provider_message.lower()
    )
    if code == "VLM-005" and no_compatible_openrouter_endpoint:
        return {
            "explanation_title": "OpenRouter 免費路由暫時沒有相容模型",
            "explanation_detail": (
                "這次請求需要圖片理解與結構化輸出；OpenRouter 回覆 HTTP 404，"
                "當下沒有免費端點可同時支援。可稍後重跑，或改用固定且支援 Vision／JSON Schema 的模型。"
            ),
            "explanation_tone": "error",
        }
    if code == "VLM-005":
        return {
            "explanation_title": "目前沒有可用的模型端點",
            "explanation_detail": (
                "Provider 可能正在冷卻、忙碌或已達 Rate Limit；稍後重跑，或改用較穩定的固定 Vision 模型。"
            ),
            "explanation_tone": "error",
        }
    raw_message = str(item["error_message"] or "").strip()
    if code == "VLM-004" and "display_suitability_grade" in raw_message:
        compact_provider_response = provider_response_text.replace(" ", "")
        nullable_grade_was_rejected = (
            '"display_suitability_grade":null' in compact_provider_response
            or '\\"display_suitability_grade\\":null' in compact_provider_response
        )
        return {
            "explanation_title": (
                "舊版把『無法判斷 E6 等級』誤判為格式錯誤"
                if nullable_grade_was_rejected
                else "模型輸出的 E6 適合度等級格式錯誤"
            ),
            "explanation_detail": (
                "模型回傳 null，表示無法判斷；舊版驗證器與 Schema 規則不一致而拒絕儲存。"
                "目前版本已接受 null；照片檔本身沒有損壞，可重跑這筆舊失敗。"
                if nullable_grade_was_rejected
                else "模型回傳的 display_suitability_grade 不是允許的 S、A、B、C、D、E 或 unknown；"
                "系統修復一次後仍不合格，因此拒絕儲存。照片檔本身沒有損壞。"
            ),
            "explanation_tone": "error",
        }
    if code == "VLM-004":
        return {
            "explanation_title": "模型輸出不符合分析格式",
            "explanation_detail": f"模型修復一次後仍無法通過欄位驗證：{raw_message or '格式不合法'}。",
            "explanation_tone": "error",
        }
    return {
        "explanation_title": "處理失敗" if not code else f"處理失敗（{code}）",
        "explanation_detail": (
            "模型服務拒絕了請求；請查看模型呼叫紀錄取得 HTTP 狀態與 Provider 回覆。"
            if raw_message == "ProviderHTTPError"
            else raw_message or "請查看模型呼叫紀錄取得詳細原因。"
        ),
        "explanation_tone": "error",
    }


def _job_performance_view(job) -> dict:
    settings_repository = current_app.extensions["inktime_settings_repository"]
    runtime_config = current_app.extensions["inktime_runtime_config"]
    try:
        job_settings = json.loads(str(job["settings_json"] or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        job_settings = {}
    configured_concurrency = max(
        1,
        int(job_settings.get("concurrency", settings_repository.get("analysis.concurrency"))),
    )
    worker_cap = max(1, int(runtime_config.worker_concurrency))
    effective_concurrency = min(configured_concurrency, worker_cap)
    with current_app.extensions["inktime_database"].session() as connection:
        timing = connection.execute(
            """
            SELECT COUNT(*) samples,
                   AVG((julianday(completed_at)-julianday(started_at))*86400.0) average_seconds
            FROM job_items
            WHERE job_id=? AND status='completed'
              AND stage IN ('single','stage_one','stage_two')
              AND started_at IS NOT NULL AND completed_at IS NOT NULL
            """,
            (str(job["id"]),),
        ).fetchone()
    average_seconds = float(timing["average_seconds"] or 0)
    estimated_days = (
        average_seconds * 100_000 / effective_concurrency / 86_400 if average_seconds > 0 else None
    )
    return {
        "configured_concurrency": configured_concurrency,
        "worker_cap": worker_cap,
        "effective_concurrency": effective_concurrency,
        "samples": int(timing["samples"] or 0),
        "average_seconds": round(average_seconds, 1) if average_seconds > 0 else None,
        "estimated_100k_days": round(estimated_days, 1) if estimated_days is not None else None,
    }


def _job_status_view(job) -> dict:
    job_id = str(job["id"])
    status = str(job["status"])
    with current_app.extensions["inktime_database"].session() as connection:
        rows = connection.execute(
            "SELECT status,stage,COUNT(*) count FROM job_items WHERE job_id=? GROUP BY status,stage",
            (job_id,),
        ).fetchall()
        budget_candidate = connection.execute(
            """
            SELECT photo_id FROM job_items
            WHERE job_id=? AND status='pending' AND started_at IS NOT NULL
            ORDER BY started_at DESC,id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        budget_unknown = (
            connection.execute(
                """
                SELECT provider_id,provider,model FROM api_usage
                WHERE photo_id=? AND cost_source='unknown'
                ORDER BY id DESC LIMIT 1
                """,
                (budget_candidate["photo_id"],),
            ).fetchone()
            if budget_candidate and budget_candidate["photo_id"]
            else None
        )
    counts: dict[str, int] = {}
    stage_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        status_key = str(row["status"])
        count = int(row["count"])
        counts[status_key] = counts.get(status_key, 0) + count
        stage_counts[(status_key, str(row["stage"]))] = count
    model_completed = sum(
        count
        for (item_status, item_stage), count in stage_counts.items()
        if item_status == "completed" and item_stage in {"single", "stage_one", "stage_two"}
    )
    local_completed = sum(
        count
        for (item_status, item_stage), count in stage_counts.items()
        if item_status == "completed" and item_stage in {"local", "local_fallback", "prefilter"}
    )
    reused_completed = sum(
        count
        for (item_status, item_stage), count in stage_counts.items()
        if item_status == "completed" and item_stage in {"cache", "inherited"}
    )
    completed = int(job["completed_items"] or 0)
    failed = int(job["failed_items"] or 0)
    total = int(job["total_items"] or 0)
    processed = completed + failed
    summary = {
        "pending": "工作已建立，正在等待啟動。",
        "preparing": "正在準備分析計畫與照片清單。",
        "running": "Worker 正在處理照片；此頁每 3 秒自動更新。",
        "pausing": "正在等目前的照片處理完成後暫停。",
        "paused": "工作已由使用者暫停；按「繼續處理」可恢復。",
        "retrying": "正在重新處理先前暫時失敗的項目。",
        "completed": "所有工作項目都已處理完成。",
        "completed_with_errors": "工作已結束，但仍有失敗項目可重跑。",
        "failed": "工作無法繼續；請查看下方錯誤或 AI Trace。",
        "cancelled": "工作已取消；尚未處理的照片不會送出。",
        "budget_exceeded": "工作已由成本安全規則暫停，尚未完成的照片仍保留在 Queue。",
    }.get(status, "工作狀態已更新。")
    action_url = None
    action_label = None
    blocker_code = None
    if status == "budget_exceeded":
        snapshot = current_app.extensions["inktime_budget_service"].snapshot(
            job_id,
            (
                str(budget_candidate["photo_id"])
                if budget_candidate and budget_candidate["photo_id"]
                else None
            ),
        )
        if int(snapshot.get("photo_unknown_count", 0)) or int(snapshot.get("job_unknown_count", 0)):
            blocker_code = "HISTORICAL-UNKNOWN"
            unknown_label = (
                f"{budget_unknown['provider']} / {budget_unknown['model']}"
                if budget_unknown
                else "先前模型請求"
            )
            summary = (
                f"這個工作曾被舊版規則暫停：{unknown_label} 留下 unknown 成本。"
                "unknown 現在只保留追蹤，不再阻斷照片；按「繼續處理」即可恢復。"
            )
        elif snapshot.get("job_limit") is not None and float(
            snapshot.get("job_effective", 0)
        ) >= float(snapshot["job_limit"]):
            blocker_code = "BUDGET-001"
            summary = "這個工作的預算上限已用完；提高預算或建立新工作後才能繼續。"
        else:
            blocker_code = "BUDGET-002"
            summary = "每日、每月或單張照片的成本安全上限已觸發，工作已暫停。"
            action_url = "/settings?search=budget"
            action_label = "查看預算設定"
    tone = (
        "success"
        if status == "completed"
        else "error"
        if status in {"failed", "completed_with_errors"}
        else "warning"
        if status in {"paused", "pausing", "budget_exceeded", "cancelled"}
        else "active"
        if status in {"running", "retrying", "preparing"}
        else "neutral"
    )
    return {
        "status": status,
        "status_label": JOB_STATUS_LABELS.get(status, status),
        "status_summary": summary,
        "status_tone": tone,
        "blocker_code": blocker_code,
        "action_url": action_url,
        "action_label": action_label,
        "processed_items": processed,
        "pending_items": counts.get("pending", 0),
        "running_items": counts.get("running", 0),
        "model_completed_items": model_completed,
        "local_completed_items": local_completed,
        "reused_items": reused_completed,
        "progress_percent": round(processed * 100 / total) if total else 0,
        "last_activity_at": job["heartbeat_at"] or job["created_at"],
    }


def _service() -> JobService:
    return current_app.extensions["inktime_job_service"]


def _repository():
    return current_app.extensions["inktime_job_repository"]


def _payload() -> dict:
    return json_object_payload(request, maximum_bytes=256 * 1024, error_prefix="JOB-001")


def _analysis_plan(strategy: str) -> tuple[dict, str]:
    analysis = current_app.extensions["inktime_analysis_service"]
    settings = current_app.extensions["inktime_settings_repository"]
    scoring = dict(current_app.extensions["inktime_scoring_repository"].current())
    mode = execution_mode(settings)
    provider_route = []
    if strategy != "local":
        if not permits_automatic_ai(mode):
            raise ValueError("目前分析模式不允許建立模型工作")
        provider_route = current_app.extensions["inktime_provider_service"].usable_route_snapshot()
        if not provider_route:
            raise ValueError("目前沒有已啟用且設定完整的 Vision Provider")
    plan = analysis.build_plan(
        strategy=strategy,
        provider_route=provider_route,
        scoring_profile=scoring,
    )
    return plan, canonical_json(plan)


def _job_or_404(job_id: str):
    repository = _repository()
    job = repository.get(job_id)
    if job is None or not repository.can_access(
        job_id,
        str(g.user["id"]),
        administrator=str(g.user["role"]) == "administrator",
    ):
        abort(404)
    return job


def _available_job_actions(job) -> list[str]:
    status = str(job["status"])
    actions: list[str] = []
    if status == "pending":
        actions.append("start")
    if status in {"running", "retrying"}:
        actions.append("pause")
    if status in {"paused", "budget_exceeded"}:
        actions.append("resume")
    if status not in {"completed", "completed_with_errors", "failed", "cancelled"}:
        actions.append("cancel")
    if status in {"failed", "completed_with_errors"} and int(job["failed_items"] or 0) > 0:
        actions.append("retry-failed")
    return actions


@bp.get("/jobs")
@login_required
def jobs_page():
    jobs = (
        _repository().list()
        if str(g.user["role"]) == "administrator"
        else _repository().list_for_user(str(g.user["id"]))
    )
    settings_repository = current_app.extensions["inktime_settings_repository"]
    provider_service = current_app.extensions["inktime_provider_service"]
    mode = execution_mode(settings_repository)
    usable_routes = provider_service.usable_route_snapshot()
    automatic_ai_enabled = permits_automatic_ai(mode)
    model_provider_available = bool(automatic_ai_enabled and usable_routes)
    if not automatic_ai_enabled:
        mode_label = {
            "disabled": "完全停用",
            "local_only": "僅使用本機選片",
            "local_with_manual_ai": "本機選片＋手動 AI",
        }.get(mode, mode)
        model_provider_message = (
            f"目前分析執行模式是「{mode_label}」，因此不允許建立自動 Vision 工作。"
        )
        model_provider_action = ("前往設定並搜尋「分析執行模式」", "/settings?search=分析執行模式")
    else:
        model_provider_message = (
            "分析模式已允許自動 AI，但目前沒有同時符合啟用、Vision 能力、Base URL，"
            "以及 API Key（本機 Ollama 除外）的 Provider。"
        )
        model_provider_action = ("前往模型與 API", "/providers")
    return render_template(
        "jobs.html",
        jobs=jobs,
        job_status_labels=JOB_STATUS_LABELS,
        model_provider_available=model_provider_available,
        model_provider_message=model_provider_message if not model_provider_available else "",
        model_provider_action=model_provider_action if not model_provider_available else None,
        usable_provider_count=len(usable_routes),
        ai_readiness=ai_readiness_snapshot(
            settings_repository,
            current_app.extensions["inktime_provider_repository"],
            provider_service,
        ),
    )


@bp.get("/jobs/<job_id>")
@login_required
def job_detail(job_id: str):
    job = _job_or_404(job_id)
    total_pages = max(1, math.ceil(int(job["total_items"] or 0) / JOB_ITEM_PAGE_SIZE))
    page = min(max(1, request.args.get("page", 1, type=int)), total_pages)
    items = [
        {**dict(item), **_job_item_view(item)}
        for item in _repository().list_items(
            job_id,
            limit=JOB_ITEM_PAGE_SIZE,
            offset=(page - 1) * JOB_ITEM_PAGE_SIZE,
        )
    ]
    return render_template(
        "job_detail.html",
        job=job,
        items=items,
        job_view=_job_status_view(job),
        job_status_labels=JOB_STATUS_LABELS,
        available_actions=_available_job_actions(job),
        performance=_job_performance_view(job),
        page=page,
        total_pages=total_pages,
        range_start=(page - 1) * JOB_ITEM_PAGE_SIZE + 1 if items else 0,
        range_end=(page - 1) * JOB_ITEM_PAGE_SIZE + len(items),
    )


@bp.post("/api/v1/jobs")
@administrator_required
def create_job():
    payload = _payload()
    raw_settings = payload.get("settings", {})
    if type(raw_settings) is not dict:
        return {"message": "JOB-001 settings 必須是 JSON 物件"}, 400
    settings = dict(raw_settings)
    try:
        budget = nullable_json_float(
            payload,
            "budget_limit",
            minimum=0,
            maximum=1_000_000,
            error_prefix="JOB-001",
        )
        limit = optional_json_int(
            payload,
            "limit",
            minimum=1,
            maximum=100_000,
            error_prefix="JOB-001",
        )
        force_recompute = json_bool(
            payload,
            "force_recompute",
            default=str(payload.get("selection_mode", "pending")) == "force_all",
            error_prefix="JOB-001",
        )
    except JsonScalarError as exc:
        return {"message": str(exc)}, 400
    selection_mode = str(payload.get("selection_mode", "pending"))
    if selection_mode not in {"pending", "stale_only", "force_all"}:
        return {"message": "不支援的選片模式"}, 400
    if selection_mode == "force_all" and str(g.user["role"]) != "administrator":
        return {"message": "force_all 僅限管理員"}, 403
    try:
        strategy = normalize_analysis_strategy(payload.get("strategy", "single"))
    except ValueError as exc:
        return {"message": str(exc)}, 400
    # Creating a Vision job is an explicit administrator action.  Every
    # eligible photo frozen into that job must therefore cross the Provider
    # boundary; the background top-candidate policy is for automatic runs and
    # must not silently turn most manually queued items into local_fallback.
    settings["force_ai"] = strategy != "local"
    settings["source"] = "manual-job"
    if execution_mode(current_app.extensions["inktime_settings_repository"]) == "disabled":
        return {
            "error_code": "ANALYSIS-DISABLED",
            "message": "目前分析執行模式為完全停用，不會建立新的分析工作。",
        }, 409
    try:
        plan, _ = _analysis_plan(strategy)
    except ValueError as exc:
        return {"error_code": "VLM-001", "message": str(exc)}, 409
    analysis_fingerprint = fingerprint(plan)
    idempotency_key = scoped_idempotency_key("analysis", str(g.user["id"]), request.headers.get("Idempotency-Key"))
    idempotency_fingerprint = (
        request_fingerprint(
            {
                "name": str(payload.get("name", "分析工作")),
                "strategy": strategy,
                "settings": settings,
                "budget_limit": budget,
                "limit": limit,
                "photo_ids": payload.get("photo_ids"),
                "selection_mode": selection_mode,
                "analysis_fingerprint": analysis_fingerprint,
                "force_recompute": force_recompute,
                "analysis_spec": plan,
            }
        )
        if idempotency_key
        else None
    )
    try:
        job_id = _service().create_analysis_job(
            name=str(payload.get("name", "分析工作")),
            strategy=strategy,
            settings=settings,
            created_by=g.user["id"],
            budget_limit=budget,
            limit=limit,
            photo_ids=payload.get("photo_ids"),
            selection_mode=selection_mode,
            analysis_fingerprint=analysis_fingerprint,
            force_recompute=force_recompute,
            analysis_spec=plan,
            dedupe_key=idempotency_key,
            request_fingerprint=idempotency_fingerprint,
        )
    except ValueError as exc:
        if str(exc) == "IDEMPOTENCY_CONFLICT":
            return {"error_code": "IDEMPOTENCY_CONFLICT", "message": str(exc)}, 409
        return {"message": str(exc)}, 409
    return {"id": job_id, "detail_url": f"/jobs/{job_id}"}, 201


@bp.post("/api/v1/jobs/selection-preview")
@administrator_required
def selection_preview():
    payload = _payload()
    mode = str(payload.get("selection_mode", "pending"))
    if mode not in {"pending", "stale_only", "force_all"}:
        return {"message": "不支援的選片模式"}, 400
    try:
        limit = optional_json_int(
            payload,
            "limit",
            minimum=1,
            maximum=100_000,
            error_prefix="JOB-001",
        )
    except JsonScalarError as exc:
        return {"message": str(exc)}, 400
    try:
        strategy = normalize_analysis_strategy(payload.get("strategy", "single"))
    except ValueError as exc:
        return {"message": str(exc)}, 400
    if execution_mode(current_app.extensions["inktime_settings_repository"]) == "disabled":
        return {
            "error_code": "ANALYSIS-DISABLED",
            "message": "目前分析執行模式為完全停用，不會建立新的分析工作。",
        }, 409
    try:
        _plan, _ = _analysis_plan(strategy)
    except ValueError as exc:
        return {"error_code": "VLM-001", "message": str(exc)}, 409
    preview = _repository().selection_preview(
        analysis_fingerprint=fingerprint(_plan),
        selection_mode=mode,
        limit=limit,
    )
    estimate = _service().estimate(int(preview["limited_to"]), strategy)
    return {
        **preview,
        "image_calls": estimate["image_calls"],
        "estimated_stage_one": estimate["stage_one_photos"],
        "estimated_stage_two": estimate["stage_two_photos"],
        "estimated_cost": estimate["average_cost"],
    }


@bp.post("/api/v1/jobs/<job_id>/<action>")
@administrator_required
def control_job(job_id: str, action: str):
    _job_or_404(job_id)
    actions = {
        "start": _service().start,
        "pause": _service().pause,
        "resume": _service().resume,
        "cancel": _service().cancel,
        "retry-failed": _service().retry_failed,
    }
    function = actions.get(action)
    if function is None:
        abort(404)
    try:
        result = function(job_id)
        if action == "retry-failed":
            if not isinstance(result, int) or result < 1:
                raise InvalidJobTransition("沒有可重跑的失敗項目")
            _service().start(job_id)
    except InvalidJobTransition as exc:
        return {"error_code": exc.code, "message": str(exc)}, 409
    return {"status": "ok", "affected": result if isinstance(result, int) else None}


@bp.get("/api/v1/jobs/<job_id>")
@login_required
def job_status(job_id: str):
    job = _job_or_404(job_id)
    total_pages = max(1, math.ceil(int(job["total_items"] or 0) / JOB_ITEM_PAGE_SIZE))
    page = min(max(1, request.args.get("page", 1, type=int)), total_pages)
    items = _repository().list_items(
        job_id,
        limit=JOB_ITEM_PAGE_SIZE,
        offset=(page - 1) * JOB_ITEM_PAGE_SIZE,
    )
    view = _job_status_view(job)
    result = None
    error_code = None
    if items:
        error_code = items[0]["error_code"]
        if items[0]["result_json"]:
            try:
                result = json.loads(str(items[0]["result_json"]))
            except json.JSONDecodeError:
                result = None
    response = {
        "id": str(job["id"]),
        "kind": str(job["kind"]),
        "status": str(job["status"]),
        "completed_items": int(job["completed_items"]),
        "failed_items": int(job["failed_items"]),
        "total_items": int(job["total_items"]),
        "spent": float(job["spent"]),
        "budget_limit": float(job["budget_limit"]) if job["budget_limit"] is not None else None,
        "available_actions": _available_job_actions(job),
        "view": view,
        "page": page,
        "total_pages": total_pages,
        "items": [
            {
                "id": str(item["id"]),
                "photo_id": str(item["photo_id"]) if item["photo_id"] is not None else None,
                "status": str(item["status"]),
                "stage": str(item["stage"]),
                "attempts": int(item["attempts"]),
                "error_code": str(item["error_code"]) if item["error_code"] is not None else None,
                "error_message": str(item["error_message"]) if item["error_message"] is not None else None,
                **_job_item_view(item),
            }
            for item in items
        ],
        "result": result,
        "error_code": error_code,
    }
    return response


@bp.post("/api/v1/jobs/estimate")
@administrator_required
def estimate_job():
    payload = _payload()
    try:
        photo_count = json_int(
            payload,
            "photo_count",
            default=0,
            minimum=0,
            maximum=1_000_000,
            error_prefix="JOB-001",
        )
    except JsonScalarError as exc:
        return {"message": str(exc)}, 400
    try:
        strategy = normalize_analysis_strategy(payload.get("strategy", "single"))
    except ValueError as exc:
        return {"message": str(exc)}, 400
    return _service().estimate(photo_count, strategy)


@bp.get("/api/v1/jobs/<job_id>/export")
@login_required
def export_job(job_id: str):
    _job_or_404(job_id)

    def generate():
        yield '{"job_id":' + json.dumps(job_id) + ',"items":['
        offset = 0
        first = True
        while True:
            rows = _repository().list_items(job_id, limit=500, offset=offset)
            if not rows:
                break
            for row in rows:
                if not first:
                    yield ","
                first = False
                yield json.dumps(dict(row), ensure_ascii=False)
            offset += len(rows)
        yield "]}"

    return Response(
        stream_with_context(generate()),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="inktime-job-{job_id}.json"'},
    )
