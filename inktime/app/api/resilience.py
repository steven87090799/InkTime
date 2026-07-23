"""管理端的決策／韌性 API 與裝置離線 Queue 協定。"""

from __future__ import annotations

from pathlib import Path
from hashlib import sha256
import json

from flask import Blueprint, abort, current_app, g, render_template, request, send_file

from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("resilience", __name__)


def _repo():
    return current_app.extensions["inktime_resilience_repository"]


def _payload() -> dict:
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        abort(400, description="DECISION-001 JSON Payload 必須是物件")
    return data


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
        return _repo().submit_feedback(user_id=str(g.user["id"]), payload=_payload(), trace_id=trace_id), 201
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
        return _repo().submit_feedback(user_id=str(g.user["id"]), payload=_payload()), 201
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.patch("/api/feedback/<int:feedback_id>")
@administrator_required
def patch_feedback(feedback_id: int):
    payload = _payload()
    with current_app.extensions["inktime_database"].transaction() as connection:
        existing = connection.execute("SELECT * FROM photo_feedback WHERE id=?", (feedback_id,)).fetchone()
        if not existing:
            abort(404, description="FEEDBACK-001 找不到回饋")
        if "value" in payload:
            connection.execute(
                "UPDATE photo_feedback SET value=?,updated_at=datetime('now') WHERE id=?",
                (float(payload["value"]), feedback_id),
            )
    return {"status": "ok"}


@bp.delete("/api/feedback/<int:feedback_id>")
@administrator_required
def delete_feedback(feedback_id: int):
    with current_app.extensions["inktime_database"].transaction() as connection:
        cursor = connection.execute("DELETE FROM photo_feedback WHERE id=?", (feedback_id,))
    if cursor.rowcount != 1:
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
            FROM selection_decision_traces p JOIN selection_decision_traces s ON s.device_id IS p.device_id AND date(s.created_at)=date(p.created_at)
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
        _repo().ensure_queue(device_id, depth=int(payload.get("depth", 3)))
        release_id = str(payload.get("release_id", "")).strip()
        item = (
            _repo().enqueue_release(
                device_id=device_id,
                release_id=release_id,
                priority=int(payload.get("priority", 100)),
                display_after=payload.get("display_after"),
                expires_at=payload.get("expires_at"),
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
            if release_id
            else None
        )
    except KeyError:
        abort(404, description="QUEUE-001 找不到或停用的裝置")
    except ValueError as exc:
        abort(400, description=str(exc))
    return {"queue": _repo().queue(device_id), "item": item}, 201


def _authenticated_device():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        abort(401, description="DEVICE-001 裝置驗證失敗")
    device = current_app.extensions["inktime_device_repository"].authenticate(
        auth[7:].strip(), request.remote_addr or "unknown"
    )
    if device is None:
        abort(401, description="DEVICE-001 裝置驗證失敗")
    return device


@bp.get("/api/device/v1/queue/manifest")
def device_queue_manifest():
    device = _authenticated_device()
    return _repo().manifest(str(device["id"]), release_root=current_app.config["INKTIME_RELEASE_DIR"])


@bp.post("/api/device/queue/ack")
def device_queue_ack():
    device = _authenticated_device()
    if (request.content_length or 0) > 16 * 1024:
        abort(413, description="QUEUE-001 ACK Payload 不可超過 16 KiB")
    try:
        return _repo().queue_ack(device_id=str(device["id"]), payload=_payload())
    except PermissionError as exc:
        abort(403, description=str(exc))
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.get("/api/device/v1/queue/items/<item_id>/files/<path:filename>")
def queue_item_file(item_id: str, filename: str):
    device = _authenticated_device()
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
    try:
        path = safe_join(Path(current_app.config["INKTIME_RELEASE_DIR"]), f"{item['release_id']}/{filename}")
    except UnsafePathError:
        abort(400, description="PATH-001 路徑超出允許範圍")
    if not path.is_file() or path.name == "manifest.json":
        abort(404)
    data = path.read_bytes()
    # 檔案仍要與 Release Manifest 校驗，ID 猜測不能跨裝置取得資料。
    manifest = json.loads(
        (
            Path(current_app.config["INKTIME_RELEASE_DIR"]) / str(item["release_id"]) / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    entry = next((value for value in manifest.get("files", []) if value.get("name") == filename), None)
    if (
        not entry
        or int(entry.get("size", -1)) != len(data)
        or entry.get("sha256") != sha256(data).hexdigest()
    ):
        abort(409, description="QUEUE-002 Release 檔案完整性驗證失敗")
    return send_file(path, mimetype="application/octet-stream", conditional=True)


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
    return _repo().cleanup(dry_run=bool(_payload().get("dry_run", False)))


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
