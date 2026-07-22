from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.parse import urlencode

from flask import Blueprint, abort, current_app, g, render_template, request, send_file

from inktime.app.core.paths import safe_join
from inktime.app.domain.analysis.schema import ALLOWED_TYPES
from inktime.app.domain.analysis.scoring import (
    calculate_distinguishing_score,
    prepare_score_distribution,
    score_band,
)
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("photos", __name__)
PHOTO_PAGE_SIZE = 200


def _repository():
    return current_app.extensions["inktime_photo_repository"]


def _queue_ai(photo_ids: list[str], *, created_by: str, name: str) -> dict:
    settings = current_app.extensions["inktime_settings_repository"]
    if str(settings.get("analysis.ai_mode", "top_candidates")) == "off":
        raise ValueError("AI 模式目前為關閉；不會建立模型工作")
    if not photo_ids:
        raise ValueError("沒有可送入 AI 的照片")
    daily_limit = int(settings.get("analysis.ai_daily_photo_limit", 50))
    if _repository().ai_limit_reached(
        daily_limit=daily_limit,
        monthly_limit=int(settings.get("analysis.ai_monthly_photo_limit", 500)),
    ):
        raise ValueError("已達 AI 每日或每月照片上限；目前會保留本機選片結果")
    selected = list(dict.fromkeys(photo_ids))[:daily_limit]
    job_id = current_app.extensions["inktime_job_service"].create_analysis_job(
        name=name,
        strategy=str(settings.get("analysis.strategy", "smart_two_stage")),
        settings={"force_ai": True, "source": "photo-exclusion-management"},
        created_by=created_by,
        budget_limit=None,
        photo_ids=selected,
        priority=2,
    )
    return {"id": job_id, "queued": len(selected), "detail_url": f"/jobs/{job_id}"}


@bp.get("/photos")
@login_required
def photos_page():
    page = max(1, request.args.get("page", 1, type=int))
    offset = (page - 1) * PHOTO_PAGE_SIZE
    rows, total = _repository().search(
        query=request.args.get("q", "").strip(),
        status=request.args.get("status", "").strip(),
        photo_type=request.args.get("type", "").strip(),
        minimum_score=request.args.get("score", type=float),
        duplicate_only=request.args.get("duplicates") == "1",
        limit=PHOTO_PAGE_SIZE,
        offset=offset,
    )
    e6_weight = float(
        current_app.extensions["inktime_settings_repository"].get("render.e6_weight", 20)
    ) / 100.0
    score_distribution = prepare_score_distribution(_repository().score_population())
    photos = []
    for stored_row in rows:
        photo = dict(stored_row)
        ranking_score = photo.get("ranking_score")
        e6_score = photo.get("e6_score")
        calibrated_score = None
        percentile = None
        if ranking_score is not None:
            calibrated_score, percentile = calculate_distinguishing_score(
                float(ranking_score), score_distribution
            )
            photo["raw_ranking_score"] = round(float(ranking_score), 1)
            photo["ranking_percentile"] = percentile
            photo["score_band"] = score_band(percentile, calibrated_score)
        if ranking_score is not None and e6_score is not None:
            photo["total_score"] = round(
                float(calibrated_score) * (1.0 - e6_weight) + float(e6_score) * e6_weight,
                1,
            )
            photo["total_score_source"] = "相對校準＋E6" if percentile is not None else "模型＋E6"
        elif ranking_score is not None:
            photo["total_score"] = calibrated_score
            photo["total_score_source"] = "相對校準" if percentile is not None else "模型"
        elif e6_score is not None:
            photo["total_score"] = round(float(e6_score), 1)
            photo["total_score_source"] = "E6 暫估"
        else:
            photo["total_score"] = None
            photo["total_score_source"] = "尚未評分"
        photos.append(photo)

    total_pages = max(1, math.ceil(total / PHOTO_PAGE_SIZE))
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
    rows = _repository().search_exclusions(**filters)
    reasons = sorted({str(row["reject_reason"]) for row in rows if row["reject_reason"]})
    return render_template("excluded_photos.html", photos=rows, filters=filters, reasons=reasons)


@bp.post("/api/v1/photos/<photo_id>/exclusion")
@administrator_required
def change_exclusion(photo_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        photo = _repository().set_exclusion(
            photo_id,
            action=str(payload.get("action", "")),
            changed_by=str(g.user["id"]),
            reapply_rules=bool(payload.get("reapply_rules", False)),
        )
    except KeyError:
        abort(404)
    except ValueError as exc:
        abort(400, description=f"IMG-004 {exc}")
    return {"status": "ok", "photo": photo}


@bp.post("/api/v1/photos/exclusions/batch")
@administrator_required
def change_exclusions_batch():
    payload = request.get_json(silent=True) or {}
    photo_ids = [str(value) for value in payload.get("photo_ids", [])][:500]
    action = str(payload.get("action", ""))
    if not photo_ids or action not in {"restore", "exclude", "favorite", "candidate", "reanalyze"}:
        abort(400, description="IMG-004 批次操作不合法")
    changed = 0
    for photo_id in dict.fromkeys(photo_ids):
        try:
            _repository().set_exclusion(
                photo_id,
                action=action,
                changed_by=str(g.user["id"]),
                reapply_rules=bool(payload.get("reapply_rules", False)),
            )
            changed += 1
        except KeyError:
            continue
    return {"status": "ok", "changed": changed}


@bp.post("/api/v1/photos/<photo_id>/ai")
@administrator_required
def queue_photo_ai(photo_id: str):
    if _repository().get_with_path(photo_id) is None:
        abort(404)
    try:
        return _queue_ai([photo_id], created_by=str(g.user["id"]), name="排除照片 AI 分析"), 201
    except ValueError as exc:
        return {"error_code": "VLM-008", "message": str(exc)}, 409


@bp.post("/api/v1/photos/exclusions/ai")
@administrator_required
def queue_exclusions_ai():
    payload = request.get_json(silent=True) or {}
    photo_ids = [str(value) for value in payload.get("photo_ids", [])][:500]
    try:
        return _queue_ai(photo_ids, created_by=str(g.user["id"]), name="排除照片批次 AI 分析"), 201
    except ValueError as exc:
        return {"error_code": "VLM-008", "message": str(exc)}, 409


@bp.post("/api/v1/photos/ai/run")
@administrator_required
def queue_ai_mode_run():
    payload = request.get_json(silent=True) or {}
    settings = current_app.extensions["inktime_settings_repository"]
    mode = str(settings.get("analysis.ai_mode", "top_candidates"))
    if mode == "off":
        return {"error_code": "VLM-008", "message": "AI 模式目前為關閉"}, 409
    daily_limit = int(settings.get("analysis.ai_daily_photo_limit", 50))
    if mode == "full_library" and not bool(payload.get("confirm", False)):
        total = len(_repository().eligible_photo_ids(include_all_active=True))
        estimate = current_app.extensions["inktime_job_service"].estimate(total, str(settings.get("analysis.strategy", "smart_two_stage")))
        return {
            "error_code": "VLM-009",
            "message": "完整照片庫模式需要確認照片數量與估算成本",
            "photos": total,
            "estimate": estimate,
            "confirmation_required": True,
        }, 409
    limit = int(settings.get("analysis.ai_top_n", 50)) if mode == "top_candidates" else daily_limit
    if mode == "full_library":
        group_by = str(payload.get("batch_by", "year"))
        if group_by not in {"year", "folder"}:
            abort(400, description="IMG-004 完整照片庫分批方式不合法")
        batches = _repository().eligible_photo_batches(
            group_by=group_by, limit=daily_limit, include_all_active=True
        )
        try:
            jobs = [
                _queue_ai(ids, created_by=str(g.user["id"]), name=f"完整照片庫 AI：{group}")
                for group, ids in batches
            ]
            return {"jobs": jobs, "queued": sum(job["queued"] for job in jobs), "batch_by": group_by}, 201
        except ValueError as exc:
            return {"error_code": "VLM-008", "message": str(exc)}, 409
    selected = _repository().eligible_photo_ids(limit=limit, include_all_active=mode == "full_library")
    try:
        return _queue_ai(selected, created_by=str(g.user["id"]), name="AI 模式批次分析"), 201
    except ValueError as exc:
        return {"error_code": "VLM-008", "message": str(exc)}, 409
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
            current_app.extensions["inktime_settings_repository"].get(
                "render.location_max_distance_km", 80
            )
        ),
    )
    with current_app.extensions["inktime_database"].session() as connection:
        analysis_rows = connection.execute(
            """
            SELECT a.*,v.name AS scoring_version_name
            FROM photo_analysis a
            LEFT JOIN scoring_rule_versions v ON v.id=a.scoring_version_id
            WHERE a.photo_id=? ORDER BY a.created_at DESC
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
        analysis["origin_label"] = (
            "本機判斷" if analysis.get("provider") == "local" else "模型判斷"
        )
        if analysis.get("ranking_score") is not None:
            calibrated, percentile = calculate_distinguishing_score(
                float(analysis["ranking_score"]), score_distribution
            )
            analysis["distinguishing_score"] = calibrated
            analysis["ranking_percentile"] = percentile
            analysis["score_band"] = score_band(percentile, calibrated)
        analyses.append(analysis)
    prefilter = current_app.extensions["inktime_analysis_service"].prefilter_snapshot(photo)
    return render_template(
        "photo_detail.html",
        photo=photo,
        analyses=analyses,
        usage=usage,
        errors=errors,
        events=events,
        allowed_types=sorted(ALLOWED_TYPES),
        location_name=location_name,
        prefilter=prefilter,
    )


@bp.patch("/api/v1/photos/<photo_id>")
@administrator_required
def update_photo(photo_id: str):
    payload = request.get_json(silent=True) or {}
    types = [str(value) for value in payload.get("types", [])]
    if not types or len(types) != len(set(types)) or any(value not in ALLOWED_TYPES for value in types):
        abort(400, description="IMG-004 照片類型不合法")
    side_caption = str(payload.get("side_caption", "")).strip()
    if len(side_caption) > 120:
        abort(400, description="IMG-004 電子紙短文案不可超過 120 字")
    captured_at = str(payload.get("captured_at", "")).strip() or None
    try:
        _repository().update_manual(
            photo_id,
            favorite=bool(payload.get("favorite", False)),
            captured_at=captured_at,
            types=types,
            side_caption=side_caption,
            changed_by=str(g.user["id"]),
        )
    except KeyError:
        abort(404)
    return {"status": "ok"}


@bp.patch("/api/v1/photos/<photo_id>/crop")
@administrator_required
def update_photo_crop(photo_id: str):
    payload = request.get_json(silent=True) or {}
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


@bp.post("/api/v1/cache/clear")
@administrator_required
def clear_cache():
    removed = current_app.extensions["inktime_thumbnail_cache"].clear()
    return {"removed": removed}
