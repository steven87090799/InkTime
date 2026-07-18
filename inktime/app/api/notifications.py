from __future__ import annotations

from flask import Blueprint, abort, current_app, g, request

from inktime.app.services.notifications import WEBHOOK_SECRET_KEY
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("notifications", __name__)


@bp.get("/api/v1/notifications")
@login_required
def list_notifications():
    rows = current_app.extensions["inktime_notification_service"].list(100)
    return {"notifications": [dict(row) for row in rows]}


@bp.post("/api/v1/notifications/webhook-secret")
@administrator_required
def save_webhook_secret():
    payload = request.get_json(silent=True) or {}
    secret_store = current_app.extensions["inktime_secret_store"]
    if bool(payload.get("clear")):
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
    result = service.deliver_pending(limit=10)
    row = next((row for row in service.list(20) if int(row["id"]) == notification_id), None)
    return {
        "id": notification_id,
        "webhook_status": str(row["webhook_status"]) if row else "unknown",
        "delivery": result,
    }, 200
