from __future__ import annotations

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, url_for

from inktime.app.core.json_values import JsonScalarError, json_bool, json_object_payload
from inktime.app.services.notifications import WEBHOOK_SECRET_KEY
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("notifications", __name__)


@bp.get("/notifications")
@login_required
def notifications_page():
    try:
        page = max(1, min(int(request.args.get("page", 1)), 100000))
    except ValueError:
        abort(400, description="頁碼必須是整數")
    result = current_app.extensions["inktime_notification_service"].history(page=page)
    return render_template("notifications.html", result=result)


@bp.get("/api/v1/notifications/unread-analysis")
@login_required
def unread_analysis():
    return current_app.extensions["inktime_notification_service"].unread_analysis()


@bp.post("/notifications/<int:notification_id>/read")
@administrator_required
def mark_read(notification_id: int):
    if not current_app.extensions["inktime_notification_service"].mark_read(notification_id):
        abort(404)
    return redirect(url_for("notifications.notifications_page"))


@bp.get("/api/v1/notifications")
@login_required
def list_notifications():
    rows = current_app.extensions["inktime_notification_service"].list(100)
    return {"notifications": [dict(row) for row in rows]}


@bp.post("/api/v1/notifications/webhook-secret")
@administrator_required
def save_webhook_secret():
    payload = json_object_payload(request, maximum_bytes=16 * 1024, error_prefix="NOTIFY-001")
    secret_store = current_app.extensions["inktime_secret_store"]
    try:
        clear = json_bool(payload, "clear", default=False, error_prefix="NOTIFY-001")
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    if clear:
        secret_store.delete(WEBHOOK_SECRET_KEY)
        return {"status": "ok", "configured": False}
    token = str(payload.get("token", "")).strip()
    if not token:
        abort(400, description="NOTIFY-001 Token 不可空白；若要移除請使用清除操作")
    if len(token) > 4096:
        abort(400, description="NOTIFY-001 Token 長度不可超過 4096")
    secret_store.set(WEBHOOK_SECRET_KEY, token, str(g.user["id"]))
    return {"status": "ok", "configured": True}


@bp.post("/api/v1/notifications/test")
@administrator_required
def test_notification():
    service = current_app.extensions["inktime_notification_service"]
    notification_id = service.create_test(created_by=str(g.user["id"]))
    outcome = service.deliver_one(notification_id)
    status = str(outcome["status"])
    result = {key: int(status == key) for key in ("delivered", "retrying", "failed", "disabled")}
    return {
        "id": notification_id,
        "webhook_status": status,
        "delivery": result,
    }, 200
