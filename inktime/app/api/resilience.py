"""管理端的決策／韌性 API 與裝置離線 Queue 協定。"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, g, render_template, request

from inktime.app.api.device_auth import authenticate_device_request
from inktime.app.core.json_values import (
    JsonScalarError,
    json_bool,
    json_float,
    json_int,
    json_object_payload,
)
from inktime.app.core.paths import UnsafePathError
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("resilience", __name__)


def _repo():
    return current_app.extensions["inktime_resilience_repository"]


def _payload(*, maximum_bytes: int = 64 * 1024, error_prefix: str = "DECISION-001") -> dict:
    return json_object_payload(request, maximum_bytes=maximum_bytes, error_prefix=error_prefix)


def _feedback_payload() -> dict:
    payload = _payload(error_prefix="FEEDBACK-001")
    if "days" in payload:
        payload["days"] = json_int(
            payload,
            "days",
            minimum=1,
            maximum=3650,
            error_prefix="FEEDBACK-001",
        )
    if "value" in payload:
        payload["value"] = json_float(
            payload,
            "value",
            minimum=-1,
            maximum=1,
            error_prefix="FEEDBACK-001",
        )
    return payload


@bp.get("/decision-traces")
@login_required
def decision_traces_page():
    return render_template(
        "resilience.html", title="決策追蹤", section="traces", result=_repo().list_traces(page_size=30)
    )


@bp.get("/feedback")
@login_required
def feedback_page():
    return render_template("resilience.html", title="使用者回饋", section="feedback", result={})


@bp.get("/shadow")
@login_required
def shadow_page():
    return render_template(
        "resilience.html", title="Shadow 比較", section="shadow", result=_repo().shadow_config()
    )


@bp.get("/device-queues")
@login_required
def queues_page():
    with current_app.extensions["inktime_database"].session() as connection:
        releases = [
            dict(row)
            for row in connection.execute(
                "SELECT id,render_profile,created_at FROM releases WHERE status='published' ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        ]
    return render_template(
        "resilience.html",
        title="裝置內容佇列",
        section="queues",
        result={
            "devices": [dict(row) for row in current_app.extensions["inktime_device_repository"].list()],
            "releases": releases,
        },
    )


@bp.get("/retention")
@login_required
def retention_page():
    return render_template(
        "resilience.html",
        title="儲存與資料保留",
        section="retention",
        result={"policies": _repo().retention_policies()},
    )


@bp.get("/rollouts")
@login_required
def rollouts_page():
    result = _repo().list_rollouts(page_size=30)
    with current_app.extensions["inktime_database"].session() as connection:
        result["releases"] = [
            dict(row)
            for row in connection.execute(
                "SELECT id,render_profile,created_at FROM releases WHERE status='published' ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        ]
    return render_template("resilience.html", title="Canary 發布", section="rollouts", result=result)


@bp.get("/api/decision-traces")
@login_required
def list_traces():
    try:
        return _repo().list_traces(
            page=request.args.get("page"),
            page_size=request.args.get("page_size"),
            device_id=request.args.get("device_id"),
            mode=request.args.get("mode"),
            algorithm_version_id=request.args.get("algorithm_version_id"),
            start=request.args.get("start"),
            end=request.args.get("end"),
        )
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.get("/api/decision-traces/<trace_id>")
@login_required
def get_trace(trace_id: str):
    result = _repo().trace(trace_id)
    if result is None:
        abort(404, description="DECISION-001 找不到決策追蹤")
    return result


@bp.post("/api/decision-traces/<trace_id>/feedback")
@administrator_required
def trace_feedback(trace_id: str):
    if _repo().trace(trace_id) is None:
        abort(404, description="DECISION-001 找不到決策追蹤")
    try:
        return _repo().submit_feedback(
            user_id=str(g.user["id"]), payload=_feedback_payload(), trace_id=trace_id
        ), 201
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.get("/api/feedback")
@login_required
def feedback_list():
    # 回饋資料可從 Trace 進入；此列表僅回傳受限欄位，且一律分頁。
    try:
        page, size = _repo().pagination(request.args.get("page"), request.args.get("page_size"))
    except ValueError as exc:
        abort(400, description=str(exc))
    with current_app.extensions["inktime_database"].session() as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM photo_feedback").fetchone()[0])
        rows = connection.execute(
            "SELECT id,photo_id,device_id,decision_trace_id,feedback_type,value,expires_at,created_at,updated_at FROM photo_feedback ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
            (size, (page - 1) * size),
        ).fetchall()
    return {"items": [dict(row) for row in rows], "page": page, "page_size": size, "total": total}


@bp.post("/api/feedback")
@administrator_required
def create_feedback():
    try:
        return _repo().submit_feedback(user_id=str(g.user["id"]), payload=_feedback_payload()), 201
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.patch("/api/feedback/<int:feedback_id>")
@administrator_required
def patch_feedback(feedback_id: int):
    payload = _feedback_payload()
    with current_app.extensions["inktime_database"].transaction() as connection:
        existing = connection.execute("SELECT * FROM photo_feedback WHERE id=?", (feedback_id,)).fetchone()
        if not existing:
            abort(404, description="FEEDBACK-001 找不到回饋")
        if "value" in payload:
            connection.execute(
                "UPDATE photo_feedback SET value=?,updated_at=datetime('now') WHERE id=?",
                (payload["value"], feedback_id),
            )
    return {"status": "ok"}


@bp.delete("/api/feedback/<int:feedback_id>")
@administrator_required
def delete_feedback(feedback_id: int):
    if not _repo().delete_feedback(feedback_id):
        abort(404, description="FEEDBACK-001 找不到回饋")
    return {"status": "ok"}


@bp.get("/api/shadow/config")
@login_required
def shadow_config():
    return _repo().shadow_config()


@bp.put("/api/shadow/config")
@administrator_required
def update_shadow_config():
    try:
        return _repo().update_shadow_config(_payload(), user_id=str(g.user["id"]))
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.get("/api/shadow/comparisons")
@login_required
def shadow_comparisons():
    with current_app.extensions["inktime_database"].session() as connection:
        rows = connection.execute("""SELECT p.trace_id AS production_trace_id,s.trace_id AS shadow_trace_id,
            p.primary_photo_id AS production_primary_photo_id,s.primary_photo_id AS shadow_primary_photo_id,
            p.secondary_photo_id AS production_secondary_photo_id,s.secondary_photo_id AS shadow_secondary_photo_id,
            p.layout_mode AS production_layout_mode,s.layout_mode AS shadow_layout_mode,p.fit_mode AS production_fit_mode,s.fit_mode AS shadow_fit_mode,
            p.selected_score AS production_score,s.selected_score AS shadow_score,p.duration_ms AS production_duration_ms,s.duration_ms AS shadow_duration_ms
            FROM selection_decision_traces p JOIN selection_decision_traces s ON s.correlation_key=p.correlation_key
            WHERE p.execution_mode='production' AND s.execution_mode='shadow' ORDER BY p.created_at DESC LIMIT 100""").fetchall()
    return {"items": [dict(row) for row in rows]}


@bp.get("/api/devices/<device_id>/queue")
@login_required
def get_queue(device_id: str):
    result = _repo().queue(device_id)
    if result is None:
        abort(404, description="QUEUE-001 找不到裝置內容佇列")
    return result


@bp.post("/api/devices/<device_id>/queue/generate")
@administrator_required
def generate_queue(device_id: str):
    payload = _payload()
    try:
        depth = json_int(
            payload,
            "depth",
            default=3,
            minimum=1,
            maximum=14,
            error_prefix="QUEUE-001",
        )
        priority = json_int(
            payload,
            "priority",
            default=100,
            minimum=1,
            maximum=1000,
            error_prefix="QUEUE-001",
        )
        delivery_mode = str(payload.get("delivery_mode", "online_queue"))
        if delivery_mode not in {"online_queue", "offline_schedule"}:
            raise ValueError("QUEUE-005 delivery_mode 不合法")
        offline_prefetch_allowed = json_bool(
            payload,
            "offline_prefetch_allowed",
            default=delivery_mode == "offline_schedule",
            error_prefix="QUEUE-005",
        )
        _repo().ensure_queue(device_id, depth=depth)
        release_id = str(payload.get("release_id", "")).strip()
        item = (
            _repo().enqueue_release(
                device_id=device_id,
                release_id=release_id,
                priority=priority,
                display_after=payload.get("display_after"),
                expires_at=payload.get("expires_at"),
                idempotency_key=request.headers.get("Idempotency-Key"),
                delivery_mode=delivery_mode,
                offline_prefetch_allowed=offline_prefetch_allowed,
                offline_slot=str(payload.get("offline_slot", "")).strip() or None,
                ack_deadline=str(payload.get("ack_deadline", "")).strip() or None,
            )
            if release_id
            else None
        )
    except KeyError:
        abort(404, description="QUEUE-001 找不到或停用的裝置")
    except ValueError as exc:
        abort(400, description=str(exc))
    return {"queue": _repo().queue(device_id), "item": item}, 201


@bp.get("/api/device/v1/queue/manifest")
def device_queue_manifest():
    device = authenticate_device_request()
    return current_app.extensions["inktime_device_queue_manifest_service"].build_manifest(
        device_id=str(device["id"]),
        profile_key=str(device["panel_profile"]),
    )


@bp.post("/api/device/queue/ack")  # legacy compatibility alias
@bp.post("/api/device/v1/queue/ack")
def device_queue_ack():
    device = authenticate_device_request()
    try:
        payload = _payload(maximum_bytes=16 * 1024, error_prefix="QUEUE-001")
        payload["queue_version"] = json_int(
            payload,
            "queue_version",
            required=True,
            minimum=0,
            maximum=2_147_483_647,
            error_prefix="QUEUE-001",
        )
        if "display_skipped" in payload:
            payload["display_skipped"] = json_bool(
                payload,
                "display_skipped",
                error_prefix="QUEUE-001",
            )
        skip_reason = str(payload.get("skip_reason", "")).strip()
        if payload.get("display_skipped") is True and skip_reason != "same_sha256":
            raise ValueError("QUEUE-001 skip_reason 必須是 same_sha256")
        if payload.get("display_skipped") is not True and skip_reason:
            raise ValueError("QUEUE-001 未 skip 時不得提供 skip_reason")
        if payload.get("display_skipped") is True and payload.get("event") != "DISPLAY_COMPLETED":
            raise ValueError("QUEUE-001 display_skipped 僅可用於 DISPLAY_COMPLETED")
        if len(skip_reason) > 64:
            raise ValueError("QUEUE-001 skip_reason 過長")
        payload["skip_reason"] = skip_reason
        ack_mode = str(payload.get("ack_mode", "")).strip()
        if ack_mode and ack_mode != "delayed_terminal":
            raise ValueError("QUEUE-005 ack_mode 不合法")
        if ack_mode == "delayed_terminal":
            if payload.get("event") not in {"DISPLAY_COMPLETED", "DISPLAY_FAILED"}:
                raise ValueError("QUEUE-005 delayed_terminal 僅可用於終端顯示 ACK")
            release_id = str(payload.get("release_id", "")).strip()
            if not release_id or len(release_id) > 128:
                raise ValueError("QUEUE-005 delayed_terminal 必須帶合法 release_id")
        payload["ack_mode"] = ack_mode
        return _repo().queue_ack(device_id=str(device["id"]), payload=payload)
    except PermissionError as exc:
        abort(403, description=str(exc))
    except ValueError as exc:
        status = 409 if str(exc).startswith("QUEUE-003") else 400
        abort(status, description=str(exc))


@bp.get("/api/device/v1/queue/items/<item_id>/files/<path:filename>")
def queue_item_file(item_id: str, filename: str):
    device = authenticate_device_request()
    queue = _repo().queue(str(device["id"]))
    item = next(
        (
            entry
            for entry in (queue or {}).get("items", [])
            if str(entry["id"]) == item_id and str(entry["status"]) not in {"CANCELLED", "EXPIRED", "FAILED"}
        ),
        None,
    )
    if item is None:
        abort(403, description="QUEUE-002 Queue Item 不屬於此裝置或已失效")
    service = current_app.extensions["inktime_device_release_service"]
    authorization = service.authorize_release_for_device(
        device_id=str(device["id"]),
        profile_key=str(device["panel_profile"]),
        release_id=str(item["release_id"]),
    )
    if not authorization.allowed:
        abort(404, description="QUEUE-002 Queue Item 不存在或已失效")
    try:
        data, entry = service.read_payload(authorization, filename)
    except (FileNotFoundError, UnsafePathError):
        abort(404)
    except ValueError:
        abort(409, description="QUEUE-002 Release 檔案完整性驗證失敗")
    response = current_app.response_class(data, mimetype="application/octet-stream")
    response.content_length = len(data)
    response.set_etag(str(entry["sha256"]))
    return response


@bp.get("/api/retention/policies")
@login_required
def retention_policies():
    return {"items": _repo().retention_policies()}


@bp.put("/api/retention/policies/<data_type>")
@administrator_required
def update_retention(data_type: str):
    try:
        return _repo().update_retention(data_type, _payload())
    except KeyError:
        abort(404, description="RETENTION-001 找不到保留策略")
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.post("/api/retention/dry-run")
@administrator_required
def retention_dry_run():
    return _repo().cleanup(dry_run=True)


@bp.post("/api/retention/run")
@administrator_required
def retention_run():
    try:
        dry_run = json_bool(
            _payload(),
            "dry_run",
            default=False,
            error_prefix="RETENTION-001",
        )
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    return _repo().cleanup(dry_run=dry_run)


@bp.get("/api/rollouts")
@login_required
def list_rollouts():
    try:
        return _repo().list_rollouts(page=request.args.get("page"), page_size=request.args.get("page_size"))
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.post("/api/rollouts")
@administrator_required
def create_rollout():
    payload = _payload()
    try:
        return _repo().create_rollout(
            release_id=str(payload.get("release_id", "")).strip(),
            name=str(payload.get("name", "Canary 發布")),
            user_id=str(g.user["id"]),
            stages=payload.get("stages") if isinstance(payload.get("stages"), list) else None,
        ), 201
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.get("/api/rollouts/<rollout_id>")
@login_required
def get_rollout(rollout_id: str):
    result = _repo().rollout(rollout_id)
    if result is None:
        abort(404, description="ROLLOUT-001 找不到發布活動")
    return result


def _transition(rollout_id: str, target: str):
    try:
        return _repo().transition_rollout(
            rollout_id,
            target=target,
            actor_id=str(g.user["id"]),
            reason=str(_payload().get("reason", "") or ""),
        )
    except KeyError:
        abort(404, description="ROLLOUT-001 找不到發布活動")
    except ValueError as exc:
        abort(409, description=str(exc))


@bp.post("/api/rollouts/<rollout_id>/start")
@administrator_required
def start_rollout(rollout_id: str):
    try:
        return _repo().start_rollout(rollout_id, actor_id=str(g.user["id"]))
    except KeyError:
        abort(404, description="ROLLOUT-001 找不到發布活動")
    except ValueError as exc:
        abort(409, description=str(exc))


@bp.post("/api/rollouts/<rollout_id>/approve")
@administrator_required
def approve_rollout(rollout_id: str):
    return _transition(rollout_id, "EXPANDING")


@bp.post("/api/rollouts/<rollout_id>/pause")
@administrator_required
def pause_rollout(rollout_id: str):
    return _transition(rollout_id, "PAUSED")


@bp.post("/api/rollouts/<rollout_id>/resume")
@administrator_required
def resume_rollout(rollout_id: str):
    return _transition(rollout_id, "OBSERVING")


@bp.post("/api/rollouts/<rollout_id>/rollback")
@administrator_required
def rollback_rollout(rollout_id: str):
    return _transition(rollout_id, "ROLLING_BACK")
