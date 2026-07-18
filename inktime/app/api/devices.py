from __future__ import annotations

from datetime import datetime
import json
import logging
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, abort, current_app, render_template, request

from inktime.app.core.paths import UnsafePathError, safe_join
from inktime.app.core.logging import log_event
from inktime.app.domain.rendering import DISPLAY_PROFILES
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("devices", __name__)
LOGGER = logging.getLogger("device")
SCHEDULE_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


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


def _validated_device_fields(payload, *, defaults: dict | None = None) -> dict:
    defaults = defaults or {}
    timezone_name = str(payload.get("timezone", defaults.get("timezone", "Asia/Taipei")))
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        abort(400, description="DEVICE-003 時區不是有效的 IANA 時區")
    try:
        rotation = int(payload.get("rotation", defaults.get("rotation", 0)))
    except (TypeError, ValueError):
        abort(400, description="DEVICE-003 畫面旋轉角度格式錯誤")
    if rotation not in {0, 180}:
        abort(400, description="DEVICE-003 目前正式韌體的旋轉角度只支援 0、180")
    schedule = str(payload.get("schedule", defaults.get("schedule", "08:00")))
    if not SCHEDULE_PATTERN.fullmatch(schedule):
        abort(400, description="DEVICE-003 排程必須使用 00:00 到 23:59 格式")
    name = str(payload.get("name", "")).strip()
    if not name:
        abort(400, description="DEVICE-003 裝置名稱不可空白")
    enabled_value = payload.get("enabled", defaults.get("enabled", True))
    if isinstance(enabled_value, str):
        enabled = enabled_value.lower() in {"1", "true", "yes", "on"}
    else:
        enabled = bool(enabled_value)
    panel_profile = str(
        payload.get("panel_profile", defaults.get("panel_profile", "safe_4c"))
    )
    if panel_profile not in DISPLAY_PROFILES:
        abort(400, description="DEVICE-003 不支援的電子紙面板 Profile")
    return {
        "name": name,
        "enabled": enabled,
        "timezone_name": timezone_name,
        "schedule": schedule,
        "rotation": rotation,
        "panel_profile": panel_profile,
    }


@bp.get("/devices")
@login_required
def devices_page():
    settings = current_app.extensions["inktime_settings_repository"]
    return render_template(
        "devices.html",
        devices=_repository().list(),
        device_events=_repository().list_events(100),
        notifications=current_app.extensions["inktime_notification_service"].list(100),
        display_profiles=DISPLAY_PROFILES,
        device_defaults={
            "timezone": str(settings.get("device.default_timezone", "Asia/Taipei")),
            "schedule": str(settings.get("device.default_schedule", "08:00")),
            "rotation": int(settings.get("device.default_rotation", 0)),
            "panel_profile": str(settings.get("device.default_panel_profile", "safe_4c")),
        },
    )


@bp.post("/api/v1/devices")
@administrator_required
def create_device():
    payload = request.get_json(silent=True) or request.form
    settings = current_app.extensions["inktime_settings_repository"]
    fields = _validated_device_fields(
        payload,
        defaults={
            "timezone": str(settings.get("device.default_timezone", "Asia/Taipei")),
            "schedule": str(settings.get("device.default_schedule", "08:00")),
            "rotation": int(settings.get("device.default_rotation", 0)),
            "panel_profile": str(settings.get("device.default_panel_profile", "safe_4c")),
        },
    )
    device_id, token = _repository().create(**fields)
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
    fields = _validated_device_fields(payload)
    try:
        _repository().update(device_id, **fields)
    except KeyError:
        abort(404)
    return {"status": "ok"}


@bp.get("/api/device/v1/releases/latest")
def latest_release():
    device = _authenticated_device()
    release_root = current_app.config["INKTIME_RELEASE_DIR"]
    profile_key = str(device["panel_profile"] or "safe_4c")
    latest_pointer = release_root / f"latest.{profile_key}"
    if not latest_pointer.exists() and profile_key == "safe_4c":
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
    zone = ZoneInfo(str(device["timezone"]))
    offset = datetime.now(zone).utcoffset()
    manifest["device_config"] = {
        "schema_version": 2,
        "config_version": int(device["config_version"]),
        "panel_profile": profile_key,
        "timezone": str(device["timezone"]),
        "utc_offset_minutes": int(offset.total_seconds() // 60) if offset else 0,
        "schedule": str(device["schedule"]),
        "rotation": int(device["rotation"]),
    }
    log_event(
        LOGGER,
        logging.DEBUG,
        "裝置取得發布 Manifest",
        event="device_manifest",
        details={
            "device_id": str(device["id"]),
            "release_id": release_id,
            "render_profile": profile_key,
            "config_version": int(device["config_version"]),
        },
    )
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
    log_event(
        LOGGER,
        logging.DEBUG,
        "裝置下載發布檔案",
        event="device_download",
        details={"device_id": str(device["id"]), "release_id": release_id, "filename": path.name},
    )
    return send_file(path, mimetype="application/octet-stream", conditional=True)


@bp.post("/api/device/v1/status")
def report_status():
    device = _authenticated_device()
    payload = request.get_json(silent=True) or {}

    def optional_int(key: str, minimum: int, maximum: int) -> int | None:
        value = payload.get(key)
        if value is None:
            return None
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError):
            abort(400, description=f"DEVICE-004 {key} 必須是整數")

    def optional_float(key: str, minimum: float, maximum: float) -> float | None:
        value = payload.get(key)
        if value is None:
            return None
        try:
            return max(minimum, min(float(value), maximum))
        except (TypeError, ValueError):
            abort(400, description=f"DEVICE-004 {key} 必須是數字")

    def optional_bool(key: str) -> bool | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, bool):
            abort(400, description=f"DEVICE-004 {key} 必須是布林值")
        return value

    battery = payload.get("battery_percent")
    try:
        battery_percent = max(0.0, min(float(battery), 100.0)) if battery is not None else None
    except (TypeError, ValueError):
        abort(400, description="DEVICE-004 battery_percent 必須是數字")
    error_code = str(payload.get("error_code", "")).strip()[:64]
    error_message = str(payload.get("error_message", "")).strip()[:500]
    _repository().record_status(
        str(device["id"]),
        firmware_version=str(payload.get("firmware_version", "unknown")),
        wifi_rssi=optional_int("wifi_rssi", -127, 0),
        battery_percent=battery_percent,
        free_heap_bytes=optional_int("free_heap_bytes", 0, 2_147_483_647),
        free_psram_bytes=optional_int("free_psram_bytes", 0, 2_147_483_647),
        error_code=error_code,
        error_message=error_message,
        wake_reason=str(payload.get("wake_reason", "")),
        applied_config_version=optional_int("applied_config_version", 0, 2_147_483_647),
        details={
            "display_updated": bool(payload.get("display_updated", False)),
            "release_id": str(payload.get("release_id", ""))[:100],
            "render_profile": str(payload.get("render_profile", ""))[:100],
            "reported_panel_profile": str(payload.get("panel_profile", ""))[:100],
            "applied_config_version": payload.get("applied_config_version"),
            "board_profile": str(payload.get("board_profile", ""))[:100],
            "flash_bytes": optional_int("flash_bytes", 0, 2_147_483_647),
            "psram_bytes": optional_int("psram_bytes", 0, 2_147_483_647),
            "flash_ready": optional_bool("flash_ready"),
            "psram_ready": optional_bool("psram_ready"),
            "sd_card": optional_bool("sd_card"),
            "rtc": optional_bool("rtc"),
            "cache_status": str(payload.get("cache_status", ""))[:32],
            "pmic_type": str(payload.get("pmic_type", ""))[:32],
            "usb_power": optional_bool("usb_power"),
            "battery_voltage": optional_float("battery_voltage", 0.0, 10.0),
            "battery_percent_estimated": optional_bool("battery_percent_estimated"),
            "temperature_c": optional_float("temperature_c", -100.0, 150.0),
            "humidity_percent": optional_float("humidity_percent", 0.0, 100.0),
            "last_refresh_duration_ms": optional_int(
                "last_refresh_duration_ms", 0, 600_000
            ),
            "button_wakeup": optional_bool("button_wakeup"),
        },
    )
    log_event(
        LOGGER,
        logging.WARNING if error_code else logging.INFO,
        "ESP32 回報異常" if error_code else "ESP32 狀態回報正常",
        event="device_status",
        error_code=error_code,
        details={"device_id": str(device["id"]), "wifi_rssi": payload.get("wifi_rssi")},
    )
    return {"status": "ok"}
