from __future__ import annotations

import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, abort, current_app, render_template, request

from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("devices", __name__)


def _repository() -> DeviceRepository:
    return current_app.extensions["inktime_device_repository"]


def _bearer_token() -> str:
    value = request.headers.get("Authorization", "")
    if not value.startswith("Bearer "):
        abort(401, description="DEVICE-001 裝置驗證失敗")
    return value[7:].strip()


def _authenticated_device():
    device = _repository().authenticate(_bearer_token(), request.remote_addr or "unknown")
    if device is None:
        abort(401, description="DEVICE-001 裝置驗證失敗")
    return device


@bp.get("/devices")
@login_required
def devices_page():
    return render_template("devices.html", devices=_repository().list())


@bp.post("/api/v1/devices")
@administrator_required
def create_device():
    payload = request.get_json(silent=True) or request.form
    name = str(payload.get("name", "")).strip()
    if not name:
        abort(400, description="請輸入裝置名稱")
    device_id, token = _repository().create(name)
    return {
        "id": device_id,
        "token": token,
        "warning": "此 Token 只顯示一次，請立即安全地設定到裝置。",
    }, 201


@bp.post("/api/v1/devices/<device_id>/token")
@administrator_required
def regenerate_device_token(device_id: str):
    try:
        token = _repository().regenerate(device_id)
    except KeyError:
        abort(404)
    return {"token": token, "warning": "舊 Token 已立即撤銷；新 Token 只顯示一次。"}


@bp.patch("/api/v1/devices/<device_id>")
@administrator_required
def update_device(device_id: str):
    payload = request.get_json(silent=True) or {}
    timezone_name = str(payload.get("timezone", "Asia/Taipei"))
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        abort(400, description="DEVICE-003 時區不是有效的 IANA 時區")
    rotation = int(payload.get("rotation", 0))
    if rotation not in {0, 90, 180, 270}:
        abort(400, description="DEVICE-003 旋轉角度只支援 0、90、180、270")
    name = str(payload.get("name", "")).strip()
    if not name:
        abort(400, description="DEVICE-003 裝置名稱不可空白")
    try:
        _repository().update(
            device_id,
            name=name,
            enabled=bool(payload.get("enabled", True)),
            timezone_name=timezone_name,
            schedule=str(payload.get("schedule", "daily")),
            rotation=rotation,
        )
    except KeyError:
        abort(404)
    return {"status": "ok"}


@bp.get("/api/device/v1/releases/latest")
def latest_release():
    _authenticated_device()
    release_root = current_app.config["INKTIME_RELEASE_DIR"]
    latest_pointer = release_root / "latest"
    if not latest_pointer.exists():
        abort(404, description="目前沒有可用的發布版本")
    release_id = latest_pointer.read_text(encoding="utf-8").strip()
    try:
        manifest_path = safe_join(release_root, f"{release_id}/manifest.json")
    except UnsafePathError:
        abort(500, description="DEVICE-002 發布指標不合法")
    if not manifest_path.is_file():
        abort(404, description="找不到發布 Manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["download_base_url"] = f"/api/device/v1/releases/{release_id}/files/"
    return manifest


@bp.get("/api/device/v1/releases/<release_id>/files/<path:filename>")
def release_file(release_id: str, filename: str):
    device = _authenticated_device()
    from flask import send_file

    try:
        path = safe_join(current_app.config["INKTIME_RELEASE_DIR"], f"{release_id}/{filename}")
    except UnsafePathError:
        _repository().record_download(device["id"], release_id, False)
        abort(400, description="PATH-001 路徑超出允許範圍")
    if not path.is_file() or path.name == "manifest.json":
        _repository().record_download(device["id"], release_id, False)
        abort(404)
    _repository().record_download(device["id"], release_id, True)
    return send_file(path, mimetype="application/octet-stream", conditional=True)
