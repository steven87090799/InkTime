from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from urllib.parse import urlencode

from flask import Blueprint, abort, current_app, g, render_template, request, send_file

from inktime.app.core.json_values import (
    JsonScalarError,
    json_bool,
    json_object_payload,
    reject_unknown_fields,
)
from inktime.app.core.idempotency import (
    grouped_idempotency_key,
    request_fingerprint,
    scoped_idempotency_key,
)
from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.domain.analysis.schema import ALLOWED_TYPES
from inktime.app.domain.analysis.plan import fingerprint
from inktime.app.domain.analysis.execution_mode import execution_mode, permits_automatic_ai, permits_manual_ai
from inktime.app.domain.analysis.scoring import (
    calculate_distinguishing_score,
    prepare_score_distribution,
    score_band,
)
from inktime.app.domain.photos.quality_policy import (
    evaluate_local_quality,
    is_confirmed_screenshot,
    local_candidate_score,
)
from inktime.app.web.access import administrator_required, login_required
from inktime.app.domain.photos.orientation import original_exif_orientation, resolve_effective_orientation


bp = Blueprint("photos", __name__)
PHOTO_PAGE_SIZE = 200
IDEMPOTENCY_RESERVATION_LEASE_SECONDS = 60
IDEMPOTENCY_RESERVATION_HEARTBEAT_SECONDS = 10.0


def _repository():
    return current_app.extensions["inktime_photo_repository"]


def _payload() -> dict:
    return json_object_payload(request, maximum_bytes=256 * 1024, error_prefix="IMG-004")


def _ai_job_error(exc: ValueError) -> dict:
    code = "IDEMPOTENCY_CONFLICT" if str(exc) == "IDEMPOTENCY_CONFLICT" else "VLM-008"
    return {"error_code": code, "message": str(exc)}


def _build_analysis_plan(settings, strategy: str) -> dict:
    provider_route = current_app.extensions["inktime_provider_service"].usable_route_snapshot()
    if not provider_route:
        raise ValueError("目前沒有已啟用且設定完整的 Vision Provider")
    return current_app.extensions["inktime_analysis_service"].build_plan(
        strategy=strategy,
        provider_route=provider_route,
        scoring_profile=dict(current_app.extensions["inktime_scoring_repository"].current()),
    )


def _queue_ai(
    photo_ids: list[str],
    *,
    created_by: str,
    name: str,
    force_ai: bool = False,
    idempotency_key: str | None = None,
    idempotency_scope: str = "photo-ai",
    analysis_plan: dict | None = None,
    frozen_photo_ids: bool = False,
) -> dict:
    settings = current_app.extensions["inktime_settings_repository"]
    mode = execution_mode(settings)
    if (force_ai and not permits_manual_ai(mode)) or (not force_ai and not permits_automatic_ai(mode)):
        raise ValueError("AI 模式目前為關閉；不會建立模型工作")
    if not photo_ids:
        raise ValueError("沒有可送入 AI 的照片")
    daily_limit = int(settings.get("analysis.ai_daily_photo_limit", 50))
    if not force_ai and _repository().ai_limit_reached(
        daily_limit=daily_limit,
        monthly_limit=int(settings.get("analysis.ai_monthly_photo_limit", 500)),
    ):
        raise ValueError("已達 AI 每日或每月照片上限；目前會保留本機選片結果")
    selected = (
        list(dict.fromkeys(photo_ids))[:500]
        if force_ai or frozen_photo_ids
        else _repository().active_eligible_requested_ids(photo_ids, limit=daily_limit)
    )
    if not selected:
        raise ValueError("沒有符合資格且可送入 AI 的照片")
    screenshot_ids = _repository().confirmed_screenshot_ids(selected)
    selected = [photo_id for photo_id in selected if photo_id not in screenshot_ids]
    if not selected:
        raise ValueError("已確認為截圖；為保護隱私與額度，禁止送入 AI 模型")
    strategy = str(settings.get("analysis.strategy", "single"))
    plan = analysis_plan if analysis_plan is not None else _build_analysis_plan(settings, strategy)
    idempotency_key_value = scoped_idempotency_key(idempotency_scope, created_by, idempotency_key)
    idempotency_fingerprint = (
        request_fingerprint(
            {
                "name": name,
                "strategy": strategy,
                "photo_ids": selected,
                "force_ai": force_ai,
                "analysis_fingerprint": fingerprint(plan),
                "analysis_spec": plan,
            }
        )
        if idempotency_key_value
        else None
    )
    job_id = current_app.extensions["inktime_job_service"].create_analysis_job(
        name=name,
        strategy=strategy,
        settings={"force_ai": force_ai, "source": "photo-exclusion-management" if force_ai else "ai-mode"},
        created_by=created_by,
        budget_limit=None,
        photo_ids=selected,
        priority=2,
        dedupe_key=idempotency_key_value,
        request_fingerprint=idempotency_fingerprint,
        analysis_fingerprint=fingerprint(plan),
        force_recompute=force_ai,
        analysis_spec=plan,
    )
    return {
        "id": job_id,
        "queued": len(selected),
        "screenshot_excluded": len(screenshot_ids),
        "detail_url": f"/jobs/{job_id}",
    }


@bp.get("/photos")
@login_required
def photos_page():
    search_parameters = {
        "query": request.args.get("q", "").strip(),
        "status": request.args.get("status", "").strip(),
        "photo_type": request.args.get("type", "").strip(),
        "minimum_score": request.args.get("score", type=float),
        "duplicate_only": request.args.get("duplicates") == "1",
    }
    _empty_rows, total = _repository().search(**search_parameters, limit=0, offset=0)
    total_pages = max(1, math.ceil(total / PHOTO_PAGE_SIZE))
    page = min(max(1, request.args.get("page", 1, type=int)), total_pages)
    offset = (page - 1) * PHOTO_PAGE_SIZE
    rows, _verified_total = _repository().search(
        **search_parameters,
        limit=PHOTO_PAGE_SIZE,
        offset=offset,
    )
    e6_weight = (
        float(current_app.extensions["inktime_settings_repository"].get("render.e6_weight", 20)) / 100.0
    )
    settings = current_app.extensions["inktime_settings_repository"]
    quality_settings = {
        key: settings.get(key, default)
        for key, default in (
            ("analysis.prefilter_enabled", True),
            ("analysis.prefilter_screenshots", True),
            ("analysis.prefilter_low_quality", True),
            ("analysis.prefilter_sensitivity", "conservative"),
            ("analysis.e6_prefilter_enabled", True),
            ("analysis.e6_min_score", 25),
        )
    }
    exclusion_labels = {
        "screenshot": "截圖",
        "document_or_receipt": "文件或收據",
        "severe_blur": "嚴重模糊／失焦",
        "resolution_too_low": "解析度過低",
        "tiny_nearly_blank": "近乎空白",
        "extreme_exposure_low_contrast": "極端曝光／低對比",
        "e6_below_threshold": "E6 適合度過低",
    }
    score_distribution = prepare_score_distribution(_repository().score_population())
    photos = []
    for stored_row in rows:
        photo = dict(stored_row)
        ranking_score = photo.get("ranking_score")
        e6_score = photo.get("e6_score")
        quality = evaluate_local_quality(photo, settings=quality_settings)
        stored_exclusion = str(photo.get("exclusion_status") or "eligible")
        hard_excluded = (
            not bool(photo.get("eligible", True))
            or quality["decision"] == "auto_excluded"
            or stored_exclusion in {"auto_excluded", "manually_excluded"}
        )
        photo["quality_decision"] = quality["decision"]
        photo["quality_reason"] = str(quality["primary_reason"])
        calibrated_score = None
        percentile = None
        if ranking_score is not None:
            calibrated_score, percentile = calculate_distinguishing_score(
                float(ranking_score), score_distribution
            )
            photo["raw_ranking_score"] = round(float(ranking_score), 1)
            photo["ranking_percentile"] = percentile
            photo["score_band"] = score_band(percentile, calibrated_score)
            photo["distinguishing_score"] = calibrated_score
        photo["selection_score"] = None
        photo["model_score"] = round(float(ranking_score), 1) if ranking_score is not None else None
        photo["e6_display_score"] = round(float(e6_score), 1) if e6_score is not None else None
        if hard_excluded:
            reason = str(photo.get("reject_reason") or quality["primary_reason"])
            photo.pop("score_band", None)
            photo["total_score"] = 0.0
            photo["selection_score"] = 0.0
            photo["total_score_source"] = f"已排除：{exclusion_labels.get(reason, reason)}"
        elif ranking_score is not None and e6_score is not None:
            photo["total_score"] = round(
                float(calibrated_score) * (1.0 - e6_weight) + float(e6_score) * e6_weight,
                1,
            )
            photo["selection_score"] = photo["total_score"]
            photo["total_score_source"] = "相對校準＋E6" if percentile is not None else "模型＋E6"
        elif ranking_score is not None:
            photo["total_score"] = calibrated_score
            photo["selection_score"] = calibrated_score
            photo["total_score_source"] = "相對校準" if percentile is not None else "模型"
        else:
            photo["total_score"] = None
            photo["local_quality_score"] = (
                local_candidate_score(photo, evaluation=quality)
                if str(photo.get("local_features_status") or "") == "complete"
                else None
            )
            photo["total_score_source"] = "尚未完成正式排序分析"
        photos.append(photo)

    filter_args = request.args.to_dict(flat=True)
    filter_args.pop("page", None)

    def page_url(target_page: int) -> str:
        return f"?{urlencode({**filter_args, 'page': target_page})}"

    return render_template(
        "photos.html",
        photos=photos,
        total=total,
        page=page,
        page_size=PHOTO_PAGE_SIZE,
        total_pages=total_pages,
        range_start=offset + 1 if photos else 0,
        range_end=offset + len(photos),
        previous_url=page_url(page - 1) if page > 1 else None,
        next_url=page_url(page + 1) if page < total_pages else None,
        filter_args=filter_args,
    )


@bp.get("/photos/excluded")
@login_required
def excluded_photos_page():
    filters = {
        "reason": request.args.get("reason", "").strip(),
        "year": request.args.get("year", "").strip(),
        "folder": request.args.get("folder", "").strip(),
        "kind": request.args.get("kind", "").strip(),
        "origin": request.args.get("origin", "").strip(),
    }
    rows = [dict(row) for row in _repository().search_exclusions(**filters)]
    for row in rows:
        row["ai_blocked"] = is_confirmed_screenshot(row)
    reasons = sorted({str(row["reject_reason"]) for row in rows if row["reject_reason"]})
    return render_template("excluded_photos.html", photos=rows, filters=filters, reasons=reasons)


@bp.post("/api/v1/photos/<photo_id>/exclusion")
@administrator_required
def change_exclusion(photo_id: str):
    payload = _payload()
    try:
        reapply_rules = json_bool(
            payload,
            "reapply_rules",
            default=False,
            error_prefix="IMG-004",
        )
        photo = _repository().set_exclusion(
            photo_id,
            action=str(payload.get("action", "")),
            changed_by=str(g.user["id"]),
            reapply_rules=reapply_rules,
        )
    except KeyError:
        abort(404)
    except (JsonScalarError, ValueError) as exc:
        abort(400, description=f"IMG-004 {exc}")
    return {"status": "ok", "photo": photo}


@bp.post("/api/v1/photos/<photo_id>/upload-privacy")
@administrator_required
def change_upload_privacy(photo_id: str):
    payload = _payload()
    try:
        reject_unknown_fields(payload, {"never_upload"}, error_prefix="IMG-005")
        value = json_bool(payload, "never_upload", required=True, error_prefix="IMG-005")
        privacy = _repository().set_upload_privacy(photo_id, never_upload=value, changed_by=str(g.user["id"]))
    except KeyError:
        abort(404)
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    return {"status": "ok", "photo": privacy}


@bp.post("/api/v1/photos/exclusions/batch")
@administrator_required
def change_exclusions_batch():
    payload = _payload()
    photo_ids = [str(value) for value in payload.get("photo_ids", [])][:500]
    action = str(payload.get("action", ""))
    if not photo_ids or action not in {"restore", "exclude", "favorite", "candidate", "reanalyze"}:
        abort(400, description="IMG-004 批次操作不合法")
    try:
        reapply_rules = json_bool(
            payload,
            "reapply_rules",
            default=False,
            error_prefix="IMG-004",
        )
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    changed = 0
    for photo_id in dict.fromkeys(photo_ids):
        try:
            _repository().set_exclusion(
                photo_id,
                action=action,
                changed_by=str(g.user["id"]),
                reapply_rules=reapply_rules,
            )
            changed += 1
        except KeyError:
            continue
    return {"status": "ok", "changed": changed}


@bp.post("/api/v1/photos/<photo_id>/ai")
@administrator_required
def queue_photo_ai(photo_id: str):
    photo = _repository().get_with_path(photo_id)
    if photo is None:
        abort(404)
    if str(photo["exclusion_status"] or "eligible") == "eligible":
        abort(403, description="IMG-004 Force AI 僅限排除照片管理操作")
    try:
        return _queue_ai(
            [photo_id],
            created_by=str(g.user["id"]),
            name="排除照片 AI 分析",
            force_ai=True,
            idempotency_scope="photo-ai",
            idempotency_key=str(request.headers.get("Idempotency-Key") or "").strip()[:128] or None,
        ), 201
    except ValueError as exc:
        return _ai_job_error(exc), 409


@bp.post("/api/v1/photos/exclusions/ai")
@administrator_required
def queue_exclusions_ai():
    payload = _payload()
    photo_ids = [str(value) for value in payload.get("photo_ids", [])][:500]
    photo_ids = [
        photo_id
        for photo_id in photo_ids
        if (photo := _repository().get_with_path(photo_id)) is not None
        and str(photo["exclusion_status"] or "eligible") != "eligible"
    ]
    try:
        return _queue_ai(
            photo_ids,
            created_by=str(g.user["id"]),
            name="排除照片批次 AI 分析",
            force_ai=True,
            idempotency_scope="exclusions-ai",
            idempotency_key=str(request.headers.get("Idempotency-Key") or "").strip()[:128] or None,
        ), 201
    except ValueError as exc:
        return _ai_job_error(exc), 409


@bp.post("/api/v1/photos/ai/run")
@administrator_required
def queue_ai_mode_run():
    payload = _payload()
    settings = current_app.extensions["inktime_settings_repository"]
    execution = execution_mode(settings)
    mode = str(settings.get("analysis.ai_mode", "top_candidates"))
    if not permits_automatic_ai(execution):
        return {"error_code": "VLM-008", "message": "AI 模式目前為關閉"}, 409
    daily_limit = int(settings.get("analysis.ai_daily_photo_limit", 50))
    try:
        confirmed = json_bool(payload, "confirm", default=False, error_prefix="VLM-009")
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    strategy = str(settings.get("analysis.strategy", "single"))
    try:
        analysis_plan = _build_analysis_plan(settings, strategy)
    except ValueError as exc:
        return _ai_job_error(exc), 409
    if mode == "full_library" and not confirmed:
        total_eligible = _repository().count_active_eligible()
        queued_now = min(total_eligible, daily_limit)
        estimate = current_app.extensions["inktime_job_service"].estimate(
            queued_now, str(settings.get("analysis.strategy", "single"))
        )
        return {
            "error_code": "VLM-009",
            "message": "完整照片庫模式需要確認照片數量與估算成本",
            "photos": queued_now,
            "eligible_total": total_eligible,
            "estimate": estimate,
            "confirmation_required": True,
        }, 409
    limit = int(settings.get("analysis.ai_top_n", 50)) if mode == "top_candidates" else daily_limit
    if mode == "full_library":
        group_by = str(payload.get("batch_by", "year"))
        if group_by not in {"year", "folder"}:
            abort(400, description="IMG-004 完整照片庫分批方式不合法")
        request_key = request.headers.get("Idempotency-Key")
        if request_key and str(request_key).strip():
            actor = str(g.user["id"])
            analysis_fingerprint = fingerprint(analysis_plan)
            request_material = {
                "analysis_fingerprint": analysis_fingerprint,
                "analysis_spec": analysis_plan,
                "batch_by": group_by,
                "confirm": bool(confirmed),
                "daily_limit": daily_limit,
                "mode": mode,
                "strategy": strategy,
            }
            request_scope = scoped_idempotency_key(
                "ai-mode-run/full-library-request", actor, request_key
            )
            request_fp = request_fingerprint(request_material)
            job_repository = current_app.extensions["inktime_job_repository"]
            try:
                # Reserve the request before touching the potentially expensive
                # library enumeration.  Only the durable reservation owner may
                # freeze the snapshot; concurrent callers either reuse a frozen
                # snapshot or retry while the owner is still enumerating.
                existing = job_repository.reserve_idempotent_request(
                    request_scope,
                    request_fp,
                    lease_seconds=IDEMPOTENCY_RESERVATION_LEASE_SECONDS,
                )
            except ValueError as exc:
                return _ai_job_error(exc), 409
            if existing is not None and str(existing["status"]) == "completed":
                return json.loads(str(existing["response_json"] or "{}")), 201

            try:
                snapshot = json.loads(str(existing["request_snapshot_json"] or "{}"))
            except json.JSONDecodeError as exc:
                raise ValueError("IDEMPOTENCY_LEDGER_INVALID") from exc
            has_frozen_snapshot = (
                isinstance(snapshot, dict)
                and isinstance(snapshot.get("batches"), list)
                and isinstance(snapshot.get("analysis_spec"), dict)
            )
            if not has_frozen_snapshot:
                if not bool(existing.get("reservation_owner")):
                    return {
                        "error_code": "IDEMPOTENCY_IN_PROGRESS",
                        "message": "相同 Idempotency-Key 的完整照片庫請求正在建立固定選片；請稍後重試",
                    }, 409
                reservation_token = str(existing.get("reservation_token") or "")
                heartbeat_stop = threading.Event()
                heartbeat_lost = threading.Event()

                def renew_reservation() -> None:
                    while not heartbeat_stop.wait(IDEMPOTENCY_RESERVATION_HEARTBEAT_SECONDS):
                        try:
                            renewed = job_repository.renew_idempotent_request(
                                request_scope,
                                request_fp,
                                reservation_token,
                                lease_seconds=IDEMPOTENCY_RESERVATION_LEASE_SECONDS,
                            )
                        except Exception:
                            renewed = False
                        if not renewed:
                            heartbeat_lost.set()
                            return

                heartbeat = threading.Thread(
                    target=renew_reservation,
                    name="inktime-idempotency-reservation-heartbeat",
                    daemon=True,
                )
                heartbeat.start()
                try:
                    batches = _repository().eligible_photo_batches(
                        group_by=group_by, limit=daily_limit, include_all_active=False
                    )
                    snapshot = {
                        "analysis_fingerprint": analysis_fingerprint,
                        "analysis_spec": analysis_plan,
                        "batch_by": group_by,
                        "batches": [
                            {"group": str(group), "photo_ids": [str(photo_id) for photo_id in ids]}
                            for group, ids in batches
                        ],
                    }
                    if heartbeat_lost.is_set() or not job_repository.renew_idempotent_request(
                        request_scope,
                        request_fp,
                        reservation_token,
                        lease_seconds=IDEMPOTENCY_RESERVATION_LEASE_SECONDS,
                    ):
                        raise ValueError("IDEMPOTENCY_RESERVATION_LOST")
                    existing = job_repository.freeze_idempotent_request(
                        request_scope,
                        request_fp,
                        reservation_token,
                        snapshot,
                        lease_seconds=IDEMPOTENCY_RESERVATION_LEASE_SECONDS,
                    )
                except ValueError as exc:
                    return _ai_job_error(exc), 409
                finally:
                    heartbeat_stop.set()
                    heartbeat.join(timeout=max(1.0, IDEMPOTENCY_RESERVATION_HEARTBEAT_SECONDS * 2))
            if str(existing["status"]) == "completed":
                return json.loads(str(existing["response_json"] or "{}")), 201
            snapshot = json.loads(str(existing["request_snapshot_json"] or "{}"))
            frozen_batches = snapshot.get("batches") if isinstance(snapshot, dict) else None
            stored_plan = snapshot.get("analysis_spec") if isinstance(snapshot, dict) else None
            if not isinstance(frozen_batches, list) or not isinstance(stored_plan, dict):
                raise ValueError("IDEMPOTENCY_LEDGER_INVALID")
            try:
                jobs = [
                    _queue_ai(
                        [str(photo_id) for photo_id in batch.get("photo_ids", [])],
                        created_by=actor,
                        name=f"完整照片庫 AI：{str(batch.get('group', '未知'))}",
                        idempotency_scope="ai-mode-run",
                        idempotency_key=grouped_idempotency_key(request_key, str(batch.get("group", "未知"))),
                        analysis_plan=stored_plan,
                        frozen_photo_ids=True,
                    )
                    for batch in frozen_batches
                ]
                response = {
                    "jobs": jobs,
                    "queued": sum(job["queued"] for job in jobs),
                    "batch_by": group_by,
                }
                completed = job_repository.complete_idempotent_request(
                    request_scope, request_fp, response
                )
                return json.loads(str(completed["response_json"] or "{}")), 201
            except ValueError as exc:
                return _ai_job_error(exc), 409

        batches = _repository().eligible_photo_batches(
            group_by=group_by, limit=daily_limit, include_all_active=False
        )
        try:
            request_key = request.headers.get("Idempotency-Key")
            jobs = [
                _queue_ai(
                    ids,
                    created_by=str(g.user["id"]),
                    name=f"完整照片庫 AI：{group}",
                    idempotency_scope="ai-mode-run",
                    idempotency_key=grouped_idempotency_key(request_key, str(group)),
                    analysis_plan=analysis_plan,
                )
                for group, ids in batches
            ]
            return {"jobs": jobs, "queued": sum(job["queued"] for job in jobs), "batch_by": group_by}, 201
        except ValueError as exc:
            return _ai_job_error(exc), 409
    selected = _repository().eligible_photo_ids(limit=limit, include_all_active=False)
    try:
        return _queue_ai(
            selected,
            created_by=str(g.user["id"]),
            name="AI 模式批次分析",
            idempotency_scope="ai-mode-run",
            idempotency_key=str(request.headers.get("Idempotency-Key") or "").strip()[:128] or None,
            analysis_plan=analysis_plan,
        ), 201
    except ValueError as exc:
        return _ai_job_error(exc), 409


@bp.get("/photos/<photo_id>")
@login_required
def photo_detail(photo_id: str):
    photo = _repository().get_with_path(photo_id)
    if photo is None:
        abort(404)
    try:
        photo = current_app.extensions["inktime_render_service"].ensure_photo_features(photo_id)
    except (OSError, ValueError):
        # 原檔暫時離線時仍允許查看既有中繼資料與模型結果。
        pass
    location_name = current_app.extensions["inktime_location_resolver"].resolve(
        photo["gps_lat"],
        photo["gps_lon"],
        max_distance_km=float(
            current_app.extensions["inktime_settings_repository"].get("render.location_max_distance_km", 80)
        ),
    )
    with current_app.extensions["inktime_database"].session() as connection:
        analysis_total = int(
            connection.execute(
                "SELECT COUNT(*) FROM photo_analysis WHERE photo_id=?", (photo_id,)
            ).fetchone()[0]
        )
        analysis_rows = connection.execute(
            """
            SELECT a.*,v.name AS scoring_version_name
            FROM photo_analysis a
            LEFT JOIN scoring_rule_versions v ON v.id=a.scoring_version_id
            WHERE a.photo_id=? ORDER BY a.created_at DESC LIMIT 2
            """,
            (photo_id,),
        ).fetchall()
        usage = connection.execute(
            "SELECT * FROM api_usage WHERE photo_id=? ORDER BY started_at DESC", (photo_id,)
        ).fetchall()
        errors = connection.execute(
            "SELECT * FROM job_errors WHERE photo_id=? ORDER BY last_seen_at DESC", (photo_id,)
        ).fetchall()
        events = connection.execute(
            "SELECT * FROM photo_events WHERE photo_id=? ORDER BY created_at DESC LIMIT 100", (photo_id,)
        ).fetchall()
    analyses = []
    score_distribution = prepare_score_distribution(_repository().score_population())
    for row in analysis_rows:
        analysis = dict(row)
        try:
            analysis["types"] = json.loads(str(analysis.get("types_json") or "[]"))
        except json.JSONDecodeError:
            analysis["types"] = []
        analysis["origin_label"] = "本機判斷" if analysis.get("provider") == "local" else "模型判斷"
        if analysis.get("ranking_score") is not None:
            calibrated, percentile = calculate_distinguishing_score(
                float(analysis["ranking_score"]), score_distribution
            )
            analysis["distinguishing_score"] = calibrated
            analysis["ranking_percentile"] = percentile
            analysis["score_band"] = score_band(percentile, calibrated)
        analyses.append(analysis)
    prefilter = current_app.extensions["inktime_analysis_service"].prefilter_snapshot(photo)
    orientation = resolve_effective_orientation(
        exif_orientation=original_exif_orientation(photo),
        manual_rotation_cw=photo["manual_orientation_rotation_cw"]
        if "manual_orientation_rotation_cw" in photo.keys()
        else None,
        ai_rotation_cw=photo["visual_orientation_rotation_cw"]
        if "visual_orientation_rotation_cw" in photo.keys()
        else None,
        ai_confidence=photo["visual_orientation_confidence"]
        if "visual_orientation_confidence" in photo.keys()
        else None,
        ai_ambiguous=bool(photo["visual_orientation_ambiguous"])
        if "visual_orientation_ambiguous" in photo.keys()
        else True,
    ).as_dict()
    return render_template(
        "photo_detail.html",
        photo=photo,
        analyses=analyses,
        analysis_total=analysis_total,
        usage=usage,
        errors=errors,
        events=events,
        allowed_types=sorted(ALLOWED_TYPES),
        location_name=location_name,
        prefilter=prefilter,
        orientation=orientation,
    )


@bp.patch("/api/v1/photos/<photo_id>")
@administrator_required
def update_photo(photo_id: str):
    payload = _payload()
    types = [str(value) for value in payload.get("types", [])]
    if not types or len(types) != len(set(types)) or any(value not in ALLOWED_TYPES for value in types):
        abort(400, description="IMG-004 照片類型不合法")
    side_caption = str(payload.get("side_caption", "")).strip()
    if len(side_caption) > 120:
        abort(400, description="IMG-004 電子紙短文案不可超過 120 字")
    captured_at = str(payload.get("captured_at", "")).strip() or None
    try:
        favorite = json_bool(payload, "favorite", default=False, error_prefix="IMG-004")
        _repository().update_manual(
            photo_id,
            favorite=favorite,
            captured_at=captured_at,
            types=types,
            side_caption=side_caption,
            changed_by=str(g.user["id"]),
        )
    except KeyError:
        abort(404)
    except (JsonScalarError, ValueError) as exc:
        abort(400, description=str(exc))
    return {"status": "ok"}


@bp.get("/api/v1/photos/<photo_id>/orientation")
@login_required
def photo_orientation_status(photo_id: str):
    photo = _repository().get_with_path(photo_id)
    if photo is None:
        abort(404)
    return resolve_effective_orientation(
        exif_orientation=original_exif_orientation(photo),
        manual_rotation_cw=photo["manual_orientation_rotation_cw"]
        if "manual_orientation_rotation_cw" in photo.keys()
        else None,
        ai_rotation_cw=photo["visual_orientation_rotation_cw"]
        if "visual_orientation_rotation_cw" in photo.keys()
        else None,
        ai_confidence=photo["visual_orientation_confidence"]
        if "visual_orientation_confidence" in photo.keys()
        else None,
        ai_ambiguous=bool(photo["visual_orientation_ambiguous"])
        if "visual_orientation_ambiguous" in photo.keys()
        else True,
    ).as_dict()


@bp.put("/api/v1/photos/<photo_id>/orientation")
@administrator_required
def update_photo_orientation(photo_id: str):
    payload = _payload()
    if not isinstance(payload, dict):
        abort(400, description="IMG-004 Request Body 必須是 JSON Object")
    if "rotation_cw" not in payload:
        abort(400, description="IMG-004 缺少 rotation_cw")
    rotation = payload["rotation_cw"]
    if isinstance(rotation, bool) or rotation not in {0, 90, 180, 270, None}:
        abort(400, description="IMG-004 旋轉角度不合法")
    try:
        _repository().set_manual_orientation(photo_id, rotation, str(g.user["id"]))
    except KeyError:
        abort(404)
    except ValueError as exc:
        abort(400, description=f"IMG-004 {exc}")
    return photo_orientation_status(photo_id)


@bp.patch("/api/v1/photos/<photo_id>/crop")
@administrator_required
def update_photo_crop(photo_id: str):
    payload = _payload()
    mode = str(payload.get("mode", "manual"))
    if mode not in {"auto", "manual"}:
        abort(400, description="RENDER-005 裁切模式不合法")
    try:
        if mode == "auto":
            _repository().update_crop(photo_id, manual_x=None, manual_y=None)
        else:
            manual_x = payload.get("x")
            manual_y = payload.get("y")
            if manual_x is None or manual_y is None:
                raise ValueError("手動裁切必須提供 X 與 Y")
            _repository().update_crop(
                photo_id,
                manual_x=float(manual_x),
                manual_y=float(manual_y),
            )
    except (TypeError, ValueError) as exc:
        abort(400, description=f"RENDER-005 {exc}")
    except KeyError:
        abort(404)
    return {"status": "ok", "mode": mode}


@bp.get("/api/v1/photos/<photo_id>/image")
@login_required
def photo_image(photo_id: str):
    photo = _repository().get_with_path(photo_id)
    if photo is None:
        abort(404)
    path = safe_join(Path(photo["root_path"]), photo["relative_path"])
    if not path.is_file():
        abort(404)
    return send_file(path, conditional=True, max_age=300)


@bp.get("/api/v1/photos/<photo_id>/thumbnail")
@login_required
def photo_thumbnail(photo_id: str):
    photo = _repository().get_with_path(photo_id)
    if photo is None or not str(photo["sha256"] or ""):
        abort(404)
    try:
        path = safe_join(Path(photo["root_path"]), photo["relative_path"])
    except UnsafePathError:
        abort(404)
    if not path.is_file():
        abort(404)
    thumbnail = current_app.extensions["inktime_thumbnail_cache"].get_or_create(
        path, str(photo["sha256"]), 512
    )
    return send_file(thumbnail, mimetype="image/jpeg", conditional=True, max_age=300)


@bp.post("/api/v1/cache/clear")
@administrator_required
def clear_cache():
    removed = current_app.extensions["inktime_thumbnail_cache"].clear()
    return {"removed": removed}
