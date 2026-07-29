from __future__ import annotations

from datetime import datetime
import logging
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, abort, current_app, render_template, request

from inktime.app.core.logging import log_event
from inktime.app.core.paths import UnsafePathError
from inktime.app.domain.rendering import DISPLAY_PROFILES, DeviceTestReleaseStore
from inktime.app.domain.rendering.system_presets import DEFAULT_DEVICE_PANEL_PROFILE
from inktime.app.services.rendering import FIT_MODES, FRAME_ORIENTATIONS, LAYOUTS
from inktime.app.repositories.devices import DeviceRateLimitError, DeviceRepository
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
    try:
        device = _repository().authenticate(_bearer_token(), request.remote_addr or "unknown")
    except DeviceRateLimitError:
        abort(429, description="DEVICE-007 裝置驗證嘗試過多，請稍後再試")
    if device is None:
        abort(401, description="DEVICE-001 裝置驗證失敗")
    return device


def optional_bool(payload: dict, field: str, *, default: bool | None = None) -> bool | None:
    if field not in payload:
        return default
    value = payload[field]
    if type(value) is not bool:
        abort(400, description=f"DEVICE-004 {field} 必須是 JSON Boolean")
    return value


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
        payload.get("panel_profile", defaults.get("panel_profile", DEFAULT_DEVICE_PANEL_PROFILE))
    )
    if panel_profile not in DISPLAY_PROFILES:
        abort(400, description="DEVICE-003 不支援的電子紙面板 Profile")
    frame_orientation = payload.get("frame_orientation", defaults.get("frame_orientation"))
    frame_orientation = str(frame_orientation).strip() if frame_orientation else None
    if frame_orientation is not None and frame_orientation not in FRAME_ORIENTATIONS:
        abort(400, description="DEVICE-003 不支援的相框方向")
    layout_mode = payload.get("layout_mode", defaults.get("layout_mode"))
    layout_mode = str(layout_mode).strip() if layout_mode else None
    if layout_mode is not None and layout_mode not in LAYOUTS:
        abort(400, description="DEVICE-003 不支援的相框版型")
    fit_mode = payload.get("fit_mode", defaults.get("fit_mode"))
    fit_mode = str(fit_mode).strip() if fit_mode else None
    if fit_mode is not None and fit_mode not in FIT_MODES:
        abort(400, description="DEVICE-003 不支援的照片縮放方式")
    return {
        "name": name,
        "enabled": enabled,
        "timezone_name": timezone_name,
        "schedule": schedule,
        "rotation": rotation,
        "panel_profile": panel_profile,
        "frame_orientation": frame_orientation,
        "layout_mode": layout_mode,
        "fit_mode": fit_mode,
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
            "panel_profile": str(settings.get("device.default_panel_profile", DEFAULT_DEVICE_PANEL_PROFILE)),
            "frame_orientation": None,
            "layout_mode": None,
            "fit_mode": None,
        },
    )


def _energy_days() -> int:
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    return days if days in {7, 30, 90, 365} else 30


@bp.get("/energy")
@login_required
def energy_page():
    devices = list(_repository().list())
    selected_id = str(request.args.get("device_id", "")).strip()
    if not selected_id and devices:
        selected_id = str(devices[0]["id"])
    energy = None
    if selected_id:
        try:
            energy = current_app.extensions["inktime_device_energy_service"].dashboard(
                selected_id, days=_energy_days()
            )
        except KeyError:
            abort(404, description="找不到能源儀表板指定的裝置")
    return render_template(
        "device_energy.html",
        devices=devices,
        selected_device_id=selected_id,
        energy=energy,
        selected_days=_energy_days(),
    )


@bp.get("/api/v1/devices/<device_id>/energy")
@login_required
def device_energy(device_id: str):
    try:
        return current_app.extensions["inktime_device_energy_service"].dashboard(
            device_id, days=_energy_days()
        )
    except KeyError:
        abort(404)


@bp.patch("/api/v1/devices/<device_id>/energy-profile")
@administrator_required
def update_energy_profile(device_id: str):
    repository = _repository()
    device = repository.get(device_id)
    if device is None:
        abort(404)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        abort(400, description="DEVICE-005 能源參數必須是 JSON 物件")

    def bounded_number(key: str, minimum: float, maximum: float, *, nullable: bool, default) -> float | None:
        value = payload.get(key, default)
        if value is None or value == "":
            if nullable:
                return None
            abort(400, description=f"DEVICE-005 {key} 不可空白")
        if isinstance(value, bool):
            abort(400, description=f"DEVICE-005 {key} 必須是數字")
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            abort(400, description=f"DEVICE-005 {key} 必須是數字")
        if not minimum <= parsed <= maximum:
            abort(400, description=f"DEVICE-005 {key} 超出 {minimum:g}–{maximum:g}")
        return parsed

    refreshes_per_day = bounded_number(
        "refreshes_per_day",
        0.01,
        96,
        nullable=False,
        default=device["refreshes_per_day"],
    )
    battery_reserve_percent = bounded_number(
        "battery_reserve_percent",
        0,
        50,
        nullable=False,
        default=device["battery_reserve_percent"],
    )
    if refreshes_per_day is None or battery_reserve_percent is None:
        abort(400, description="DEVICE-005 續航估算參數不可空白")
    try:
        repository.update_energy_profile(
            device_id,
            battery_capacity_mah=bounded_number(
                "battery_capacity_mah",
                10,
                100_000,
                nullable=True,
                default=device["battery_capacity_mah"],
            ),
            standby_current_ma=bounded_number(
                "standby_current_ma",
                0.001,
                10_000,
                nullable=True,
                default=device["standby_current_ma"],
            ),
            active_current_ma=bounded_number(
                "active_current_ma",
                0.001,
                10_000,
                nullable=True,
                default=device["active_current_ma"],
            ),
            refreshes_per_day=refreshes_per_day,
            battery_reserve_percent=battery_reserve_percent,
        )
    except KeyError:
        abort(404)
    return {"status": "ok"}


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
            "panel_profile": str(settings.get("device.default_panel_profile", DEFAULT_DEVICE_PANEL_PROFILE)),
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
    profile_key = str(device["panel_profile"] or DEFAULT_DEVICE_PANEL_PROFILE)
    authorization = current_app.extensions["inktime_device_release_service"].latest_for_device(
        device_id=str(device["id"]),
        profile_key=profile_key,
    )
    if not authorization.allowed or authorization.manifest is None:
        abort(404, description="目前沒有可用的發布版本")
    release_id = authorization.release_id
    manifest = dict(authorization.manifest)
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
    if authorization.test_assignment is not None:
        assignment = authorization.test_assignment
        manifest["test_delivery"] = {
            "mode": assignment["delivery"],
            "one_time": bool(assignment["one_time"]),
            "restore_formal": bool(assignment["restore_formal"]),
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
    profile_key = str(device["panel_profile"] or DEFAULT_DEVICE_PANEL_PROFILE)
    service = current_app.extensions["inktime_device_release_service"]
    authorization = service.authorize_release_for_device(
        device_id=str(device["id"]),
        profile_key=profile_key,
        release_id=release_id,
    )
    if not authorization.allowed:
        _repository().record_download(device["id"], release_id[:128], False)
        abort(404, description="DEVICE-002 Release 或檔案不存在")
    try:
        payload, entry = service.read_payload(authorization, filename)
    except (FileNotFoundError, UnsafePathError):
        _repository().record_download(device["id"], release_id, False)
        abort(404, description="DEVICE-002 Release 或檔案不存在")
    except ValueError:
        _repository().record_download(device["id"], release_id, False)
        abort(409, description="DEVICE-009 Release Payload 完整性驗證失敗")
    _repository().record_download(device["id"], release_id, True)
    if filename.endswith(".bin") and authorization.source == "test_assignment":
        # 只前進到 payload_downloaded；不會在 HTTP 傳輸階段 consumed。
        service.test_store.mark_downloaded(str(device["id"]), release_id)
    log_event(
        LOGGER,
        logging.DEBUG,
        "裝置下載發布檔案",
        event="device_download",
        details={"device_id": str(device["id"]), "release_id": release_id, "filename": filename},
    )
    response = current_app.response_class(payload, mimetype="application/octet-stream")
    response.content_length = len(payload)
    response.set_etag(str(entry["sha256"]))
    return response


@bp.post("/api/device/v1/status")
def report_status():
    device = _authenticated_device()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        abort(400, description="DEVICE-004 狀態 Payload 必須是 JSON 物件")
    if (request.content_length or 0) > 64 * 1024:
        abort(413, description="DEVICE-004 狀態 Payload 不可超過 64 KiB")

    def optional_int(key: str, minimum: int, maximum: int) -> int | None:
        value = payload.get(key)
        if value is None:
            return None
        if isinstance(value, bool):
            abort(400, description=f"DEVICE-004 {key} 必須是整數")
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError):
            abort(400, description=f"DEVICE-004 {key} 必須是整數")

    def optional_float(key: str, minimum: float, maximum: float) -> float | None:
        value = payload.get(key)
        if value is None:
            return None
        if isinstance(value, bool):
            abort(400, description=f"DEVICE-004 {key} 必須是數字")
        try:
            return max(minimum, min(float(value), maximum))
        except (TypeError, ValueError):
            abort(400, description=f"DEVICE-004 {key} 必須是數字")

    battery = payload.get("battery_percent")
    if isinstance(battery, bool):
        abort(400, description="DEVICE-004 battery_percent 必須是數字")
    try:
        battery_percent = max(0.0, min(float(battery), 100.0)) if battery is not None else None
    except (TypeError, ValueError):
        abort(400, description="DEVICE-004 battery_percent 必須是數字")
    error_code = str(payload.get("error_code", "")).strip()[:64]
    error_message = str(payload.get("error_message", "")).strip()[:500]
    display_updated = optional_bool(payload, "display_updated", default=False)
    payload_verified = optional_bool(payload, "payload_sha256_verified", default=False)
    assert display_updated is not None
    assert payload_verified is not None
    boolean_details = {
        key: optional_bool(payload, key)
        for key in (
            "flash_ready",
            "psram_ready",
            "sd_card",
            "rtc",
            "usb_power",
            "battery_percent_estimated",
            "button_wakeup",
        )
    }
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
            "display_updated": display_updated,
            "payload_sha256_verified": payload_verified,
            "release_id": str(payload.get("release_id", ""))[:100],
            "render_profile": str(payload.get("render_profile", ""))[:100],
            "reported_panel_profile": str(payload.get("panel_profile", ""))[:100],
            "applied_config_version": payload.get("applied_config_version"),
            "board_profile": str(payload.get("board_profile", ""))[:100],
            "flash_bytes": optional_int("flash_bytes", 0, 2_147_483_647),
            "psram_bytes": optional_int("psram_bytes", 0, 2_147_483_647),
            "flash_ready": boolean_details["flash_ready"],
            "psram_ready": boolean_details["psram_ready"],
            "sd_card": boolean_details["sd_card"],
            "rtc": boolean_details["rtc"],
            "cache_status": str(payload.get("cache_status", ""))[:32],
            "pmic_type": str(payload.get("pmic_type", ""))[:32],
            "usb_power": boolean_details["usb_power"],
            "battery_voltage": optional_float("battery_voltage", 0.0, 10.0),
            "battery_percent_estimated": boolean_details["battery_percent_estimated"],
            "temperature_c": optional_float("temperature_c", -100.0, 150.0),
            "humidity_percent": optional_float("humidity_percent", 0.0, 100.0),
            "last_refresh_duration_ms": optional_int("last_refresh_duration_ms", 0, 600_000),
            "wake_duration_ms": optional_int("wake_duration_ms", 0, 86_400_000),
            "button_wakeup": boolean_details["button_wakeup"],
        },
    )
    DeviceTestReleaseStore(current_app.config["INKTIME_RELEASE_DIR"]).confirm_display(
        str(device["id"]),
        str(payload.get("release_id", ""))[:100],
        profile_key=str(device["panel_profile"]),
        payload_verified=payload_verified,
        display_updated=display_updated,
        error_code=error_code,
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
