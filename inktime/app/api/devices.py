from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
import logging
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request

from inktime.app.api.device_auth import authenticate_device_request
from inktime.app.core.json_values import (
    JsonScalarError,
    json_bool,
    json_float,
    json_int,
    json_object_payload,
    nullable_json_float,
    nullable_json_int,
    optional_json_bool,
    optional_json_float,
    optional_json_int,
)
from inktime.app.core.logging import log_event
from inktime.app.core.paths import UnsafePathError
from inktime.app.domain.rendering import DISPLAY_PROFILES, DeviceTestReleaseStore
from inktime.app.domain.rendering.system_presets import DEFAULT_DEVICE_PANEL_PROFILE
from inktime.app.domain.photopainter.offline_schedule import (
    LEGACY_MAX_OFFLINE_SLOTS,
    MINIMUM_SCHEDULE_GAP_MINUTES,
    normalize_delivery_contract,
    normalize_sync_strategy,
    offline_schedule_capability_is_usable,
    resolve_offline_schedule_max_slots,
    validate_offline_schedule,
)
from inktime.app.services.rendering import FIT_MODES, FRAME_ORIENTATIONS, LAYOUTS
from inktime.app.services.stock_transport import UnsafeStockEndpoint, StockTransportError, validate_stock_endpoint_host
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.repositories.offline_schedules import OfflineScheduleRepository
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("devices", __name__)
LOGGER = logging.getLogger("device")
SCHEDULE_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _repository() -> DeviceRepository:
    return current_app.extensions["inktime_device_repository"]


def _offline_schedules() -> OfflineScheduleRepository:
    return current_app.extensions["inktime_offline_schedule_repository"]


def _json_payload(error_prefix: str = "DEVICE-003", *, maximum_bytes: int = 64 * 1024) -> dict:
    return json_object_payload(request, maximum_bytes=maximum_bytes, error_prefix=error_prefix)


def optional_bool(payload: dict, field: str, *, default: bool | None = None) -> bool | None:
    try:
        if field not in payload:
            return default
        return optional_json_bool(payload, field, error_prefix="DEVICE-004")
    except JsonScalarError as exc:
        abort(400, description=str(exc))


def _validated_device_fields(
    payload,
    *,
    defaults: dict | None = None,
    maximum_slots: int = LEGACY_MAX_OFFLINE_SLOTS,
) -> dict:
    defaults = defaults or {}
    timezone_name = str(payload.get("timezone", defaults.get("timezone", "Asia/Taipei")))
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        abort(400, description="DEVICE-003 時區不是有效的 IANA 時區")
    try:
        rotation = json_int(
            payload,
            "rotation",
            default=int(defaults.get("rotation", 0)),
            minimum=0,
            maximum=180,
            error_prefix="DEVICE-003",
        )
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    if rotation not in {0, 180}:
        abort(400, description="DEVICE-003 目前正式韌體的旋轉角度只支援 0、180")
    schedule = str(payload.get("schedule", defaults.get("schedule", "08:00")))
    if not SCHEDULE_PATTERN.fullmatch(schedule):
        abort(400, description="DEVICE-003 排程必須使用 00:00 到 23:59 格式")
    delivery_mode = str(payload.get("delivery_mode", defaults.get("delivery_mode", "legacy_online"))).strip()
    if delivery_mode not in {"legacy_online", "stock_compat", "inktime_offline_schedule"}:
        abort(400, description="DEVICE-008 delivery_mode 不合法")
    if "schedule_times" in payload:
        schedule_values = payload.get("schedule_times")
    elif "offline_schedule" in payload:
        schedule_values = payload.get("offline_schedule")
    elif defaults.get("delivery_mode") == "inktime_offline_schedule":
        schedule_values = defaults.get("schedule_times", defaults.get("offline_schedule", [schedule]))
    else:
        schedule_values = [schedule]
    try:
        minimum_schedule_gap_minutes = json_int(
            payload,
            "minimum_schedule_gap_minutes",
            default=int(
                defaults.get("minimum_schedule_gap_minutes", MINIMUM_SCHEDULE_GAP_MINUTES)
            ),
            minimum=30,
            maximum=360,
            error_prefix="DEVICE-008",
        )
        schedule_values = validate_offline_schedule(
            schedule_values,
            maximum=resolve_offline_schedule_max_slots(
                {"offline_schedule_max_slots": maximum_slots}
            ),
            minimum_gap_minutes=minimum_schedule_gap_minutes,
        )
    except ValueError as exc:
        abort(400, description=str(exc))
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    try:
        requested_prefetch = optional_json_bool(
            payload, "offline_prefetch_allowed", error_prefix="DEVICE-008"
        )
        delivery_mode, offline_prefetch_allowed = normalize_delivery_contract(
            delivery_mode,
            requested_prefetch,
            explicit_prefetch="offline_prefetch_allowed" in payload,
        )
    except (JsonScalarError, ValueError) as exc:
        abort(400, description=str(exc))
    try:
        stock_endpoint_host = validate_stock_endpoint_host(
            payload.get("stock_endpoint_host", defaults.get("stock_endpoint_host"))
        )
    except UnsafeStockEndpoint as exc:
        abort(400, description=str(exc))
    try:
        prefetch_lead_minutes = json_int(
            payload,
            "prefetch_lead_minutes",
            default=int(defaults.get("prefetch_lead_minutes", 5)),
            minimum=0,
            maximum=120,
            error_prefix="DEVICE-008",
        )
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    button_wake_action = str(
        payload.get("button_wake_action", defaults.get("button_wake_action", "check_new"))
    ).strip()
    if button_wake_action not in {"check_new", "local_next"}:
        abort(400, description="DEVICE-008 button_wake_action 不合法")
    sync_strategy = str(
        payload.get("sync_strategy", defaults.get("sync_strategy", "first_display_lead"))
    ).strip()
    raw_sync_time = payload.get("sync_time", defaults.get("sync_time"))
    sync_time = str(raw_sync_time).strip() if raw_sync_time not in (None, "") else None
    try:
        sync_strategy, sync_time = normalize_sync_strategy(sync_strategy, sync_time)
    except ValueError as exc:
        abort(400, description=str(exc))
    name = str(payload.get("name", defaults.get("name", ""))).strip()
    if not name:
        abort(400, description="DEVICE-003 裝置名稱不可空白")
    try:
        enabled = json_bool(
            payload,
            "enabled",
            default=bool(defaults.get("enabled", True)),
            error_prefix="DEVICE-003",
        )
    except JsonScalarError as exc:
        abort(400, description=str(exc))
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
        "delivery_mode": delivery_mode,
        "offline_prefetch_allowed": offline_prefetch_allowed,
        "schedule_times": schedule_values,
        "prefetch_lead_minutes": prefetch_lead_minutes,
        "button_wake_action": button_wake_action,
        "minimum_schedule_gap_minutes": minimum_schedule_gap_minutes,
        "sync_strategy": sync_strategy,
        "sync_time": sync_time,
        "stock_endpoint_host": stock_endpoint_host,
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
    pending_pairings = (
        current_app.extensions["inktime_device_pairing_service"].pending_for_admin()
        if getattr(g, "user", None) is not None and g.user["role"] == "administrator"
        else []
    )
    return render_template(
        "devices.html",
        devices=_repository().list(),
        pending_pairings=pending_pairings,
        device_events=_repository().list_events(100),
        notifications=current_app.extensions["inktime_notification_service"].list(100),
        display_profiles=DISPLAY_PROFILES,
        device_defaults={
            "timezone": str(settings.get("device.default_timezone", "Asia/Taipei")),
            "schedule": str(settings.get("device.default_schedule", "08:00")),
            "rotation": int(settings.get("device.default_rotation", 0)),
            "panel_profile": str(settings.get("device.default_panel_profile", DEFAULT_DEVICE_PANEL_PROFILE)),
            "delivery_mode": "stock_compat",
            "offline_prefetch_allowed": False,
            "schedule_times": [str(settings.get("device.default_schedule", "08:00"))],
            "prefetch_lead_minutes": 5,
            "button_wake_action": "check_new",
            "minimum_schedule_gap_minutes": MINIMUM_SCHEDULE_GAP_MINUTES,
            "sync_strategy": "first_display_lead",
            "sync_time": None,
            "stock_endpoint_host": None,
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


@bp.get("/api/v1/devices/<device_id>/runtime-summary")
@login_required
def device_runtime_summary(device_id: str):
    try:
        return _repository().runtime_summary(device_id)
    except KeyError:
        abort(404, description="DEVICE-002 找不到裝置")


@bp.patch("/api/v1/devices/<device_id>/energy-profile")
@administrator_required
def update_energy_profile(device_id: str):
    repository = _repository()
    device = repository.get(device_id)
    if device is None:
        abort(404)
    payload = _json_payload("DEVICE-005")
    try:
        refreshes_per_day = json_float(
            payload,
            "refreshes_per_day",
            default=device["refreshes_per_day"],
            minimum=0.01,
            maximum=96,
            error_prefix="DEVICE-005",
        )
        battery_reserve_percent = json_float(
            payload,
            "battery_reserve_percent",
            default=device["battery_reserve_percent"],
            minimum=0,
            maximum=50,
            error_prefix="DEVICE-005",
        )
        repository.update_energy_profile(
            device_id,
            battery_capacity_mah=nullable_json_float(
                payload,
                "battery_capacity_mah",
                default=device["battery_capacity_mah"],
                minimum=10,
                maximum=100_000,
                error_prefix="DEVICE-005",
            ),
            standby_current_ma=nullable_json_float(
                payload,
                "standby_current_ma",
                default=device["standby_current_ma"],
                minimum=0.001,
                maximum=10_000,
                error_prefix="DEVICE-005",
            ),
            active_current_ma=nullable_json_float(
                payload,
                "active_current_ma",
                default=device["active_current_ma"],
                minimum=0.001,
                maximum=10_000,
                error_prefix="DEVICE-005",
            ),
            refreshes_per_day=refreshes_per_day,
            battery_reserve_percent=battery_reserve_percent,
        )
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    except KeyError:
        abort(404)
    return {"status": "ok"}


@bp.post("/api/v1/devices")
@administrator_required
def create_device():
    payload = _json_payload() if request.is_json else request.form
    settings = current_app.extensions["inktime_settings_repository"]
    fields = _validated_device_fields(
        payload,
        defaults={
            "timezone": str(settings.get("device.default_timezone", "Asia/Taipei")),
            "schedule": str(settings.get("device.default_schedule", "08:00")),
            "rotation": int(settings.get("device.default_rotation", 0)),
            "panel_profile": str(settings.get("device.default_panel_profile", DEFAULT_DEVICE_PANEL_PROFILE)),
            "delivery_mode": "stock_compat",
            "schedule_times": [str(settings.get("device.default_schedule", "08:00"))],
            "prefetch_lead_minutes": 5,
            "button_wake_action": "check_new",
            "minimum_schedule_gap_minutes": MINIMUM_SCHEDULE_GAP_MINUTES,
            "sync_strategy": "first_display_lead",
            "sync_time": None,
            "stock_endpoint_host": None,
        },
    )
    is_stock = fields["delivery_mode"] == "stock_compat"
    if not is_stock:
        abort(
            409,
            description="DEVICE-011 自製 InkTime 裝置由 ESP32 首次連線建立 Pending Enrollment；只有 Stock PhotoPainter 可由管理頁新增。",
        )
    device_id, _legacy_token = _repository().create(**fields, auth_mode="stock")
    return {
        "id": device_id,
        "auth_mode": "stock",
        "pairing_state": "paired",
        "warning": "Stock PhotoPainter 相容模式不使用自動配對；伺服器維持既有 /dataUP 流程。",
    }, 201


@bp.post("/api/v1/devices/<device_id>/token")
@administrator_required
def regenerate_device_token(device_id: str):
    try:
        token = _repository().regenerate(device_id)
    except KeyError:
        abort(404)
    except ValueError as exc:
        abort(409, description=str(exc))
    return {"token": token, "warning": "舊 Token 已立即撤銷；新 Token 只顯示一次。"}


@bp.patch("/api/v1/devices/<device_id>")
@administrator_required
def update_device(device_id: str):
    payload = _json_payload()
    existing = _repository().get(device_id)
    if existing is None:
        abort(404)
    try:
        existing_schedule = json.loads(str(existing["schedule_times_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        existing_schedule = [str(existing["schedule"] or "08:00")]
    fields = _validated_device_fields(
        payload,
        defaults={
            "name": existing["name"],
            "enabled": bool(existing["enabled"]),
            "timezone": existing["timezone"],
            "schedule": existing["schedule"],
            "rotation": existing["rotation"],
            "panel_profile": existing["panel_profile"],
            "delivery_mode": existing["delivery_mode"],
            "offline_prefetch_allowed": bool(existing["offline_prefetch_allowed"]),
            "schedule_times": existing_schedule,
            "prefetch_lead_minutes": int(existing["prefetch_lead_minutes"] or 5),
            "button_wake_action": str(existing["button_wake_action"] or "check_new"),
            "minimum_schedule_gap_minutes": int(
                existing["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
            ),
            "sync_strategy": str(existing["sync_strategy"] or "first_display_lead"),
            "sync_time": existing["sync_time"],
            "stock_endpoint_host": existing["stock_endpoint_host"],
            "frame_orientation": existing["frame_orientation"],
            "layout_mode": existing["layout_mode"],
            "fit_mode": existing["fit_mode"],
        },
        maximum_slots=resolve_offline_schedule_max_slots(
            {"offline_schedule_max_slots": existing["offline_schedule_max_slots"]}
        ),
    )
    try:
        _repository().update(device_id, **fields)
    except KeyError:
        abort(404)
    except ValueError as exc:
        abort(409, description=str(exc))
    updated = _repository().get(device_id)
    if updated is None:
        abort(404)
    applied = int(updated["acked_config_version"]) >= int(updated["config_version"])
    recommended_action = None
    if str(updated["delivery_mode"] or "legacy_online") != "stock_compat" and not applied:
        recommended_action = "press_key1"
    return {
        "status": "ok",
        "config_version": int(updated["config_version"]),
        "acked_config_version": int(updated["acked_config_version"]),
        "applied": applied,
        "recommended_action": recommended_action,
        "offline_schedule_version": int(updated["offline_schedule_version"] or 0),
        "applied_offline_schedule_version": int(
            updated["applied_offline_schedule_version"] or 0
        ),
    }


@bp.get("/api/device/v1/releases/latest")
def latest_release():
    device = authenticate_device_request()
    if (
        str(device["delivery_mode"] or "legacy_online") == "inktime_offline_schedule"
        and not offline_schedule_capability_is_usable(
            device["offline_schedule_capability_state"]
        )
    ):
        abort(409, description="DEVICE-008 裝置離線 Slot 能力尚未確認，暫停離線設定傳送")
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
    if str(device["delivery_mode"] or "legacy_online") == "inktime_offline_schedule":
        manifest["device_config"].update(
            {
                "schema_version": 3,
                "delivery_mode": "inktime_offline_schedule",
                "offline_prefetch_allowed": bool(device["offline_prefetch_allowed"]),
                "offline_schedule_max_slots": int(device["offline_schedule_max_slots"] or LEGACY_MAX_OFFLINE_SLOTS),
                "offline_schedule_capability_state": str(
                    device["offline_schedule_capability_state"] or "unknown_12"
                ),
                "schedule_times": json.loads(str(device["schedule_times_json"] or "[]")),
                "prefetch_lead_minutes": int(device["prefetch_lead_minutes"] or 0),
                "button_wake_action": str(device["button_wake_action"] or "check_new"),
                "offline_schedule_version": int(device["offline_schedule_version"] or 0),
                "applied_offline_schedule_version": int(
                    device["applied_offline_schedule_version"] or 0
                ),
                "minimum_schedule_gap_minutes": int(
                    device["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
                ),
                "sync_strategy": str(device["sync_strategy"] or "first_display_lead"),
                "sync_time": device["sync_time"],
            }
        )
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


@bp.get("/api/device/v1/stock/dataUP")
def stock_data_up_payload():
    """Return the exact Stock `/dataUP` body for a bearer-authenticated bridge."""
    device = authenticate_device_request()
    if str(device["delivery_mode"] or "legacy_online") != "stock_compat":
        abort(409, description="DEVICE-008 裝置目前不是 Stock 相容模式")
    try:
        payload, metadata = current_app.extensions["inktime_stock_compatibility_service"].payload_for_latest(
            device_id=str(device["id"]),
            profile_key=str(device["panel_profile"] or DEFAULT_DEVICE_PANEL_PROFILE),
            rotate180=int(device["rotation"] or 0) == 180,
        )
    except PermissionError:
        abort(404, description="DEVICE-002 目前沒有可用 Stock Release")
    except (OSError, ValueError):
        abort(409, description="DEVICE-009 Stock Payload 完整性驗證失敗")
    response = current_app.response_class(payload, mimetype="application/octet-stream")
    response.content_length = len(payload)
    response.set_etag(str(metadata["stock_sha256"]))
    response.headers["X-InkTime-Stock-Mode"] = str(metadata["mode"])
    response.headers["X-InkTime-Source-Release"] = str(metadata["release_id"])
    return response


@bp.post("/api/v1/devices/<device_id>/stock-photopainter/display")
@administrator_required
def stock_photopainter_display(device_id: str):
    """Upload one authorized Release to the explicitly configured Stock host."""

    device = _repository().get(device_id)
    if device is None:
        abort(404, description="DEVICE-002 找不到裝置")
    if str(device["delivery_mode"] or "legacy_online") != "stock_compat":
        abort(409, description="DEVICE-008 裝置目前不是 Stock 相容模式")
    try:
        host = validate_stock_endpoint_host(device["stock_endpoint_host"])
    except UnsafeStockEndpoint as exc:
        abort(409, description=str(exc))
    if host is None:
        abort(409, description="DEVICE-009 尚未設定 Stock Host")
    payload = _json_payload("DEVICE-009", maximum_bytes=16 * 1024)
    release_id = str(payload.get("release_id", "")).strip()
    file_name = str(payload.get("file_name", "")).strip()
    if (
        not release_id
        or not file_name
        or len(release_id) > 128
        or len(file_name) > 255
        or any(marker in file_name for marker in ("/", "\\", "\x00"))
    ):
        abort(400, description="DEVICE-009 release_id 與 file_name 必須是合法單一檔名")
    service = current_app.extensions["inktime_stock_compatibility_service"]
    try:
        result = service.display_release(
            device_id=device_id,
            profile_key=str(device["panel_profile"] or DEFAULT_DEVICE_PANEL_PROFILE),
            release_id=release_id,
            file_name=file_name,
            host=host,
            rotate180=int(device["rotation"] or 0) == 180,
        )
    except PermissionError as exc:
        _repository().record_stock_upload_event(
            device_id,
            release_id=release_id,
            file_name=file_name,
            payload_bytes=0,
            status_code=None,
            upload_accepted=False,
            error_code="release_not_authorized",
        )
        abort(404, description=str(exc))
    except (FileNotFoundError, UnsafeStockEndpoint, ValueError) as exc:
        _repository().record_stock_upload_event(
            device_id,
            release_id=release_id,
            file_name=file_name,
            payload_bytes=0,
            status_code=None,
            upload_accepted=False,
            error_code="payload_invalid",
        )
        abort(409, description=str(exc))
    except StockTransportError as exc:
        _repository().record_stock_upload_event(
            device_id,
            release_id=release_id,
            file_name=file_name,
            payload_bytes=0,
            status_code=None,
            upload_accepted=False,
            error_code=exc.code,
        )
        abort(502, description=str(exc))
    _repository().record_stock_upload_event(
        device_id,
        release_id=release_id,
        file_name=file_name,
        payload_bytes=int(result["size"]),
        status_code=int(result["http_status"]),
        upload_accepted=bool(result["upload_accepted"]),
    )
    return result, 202 if result["upload_accepted"] else 502


@bp.post("/api/v1/devices/<device_id>/offline-schedule/prepare")
@administrator_required
def prepare_offline_schedule(device_id: str):
    payload = _json_payload("DEVICE-008", maximum_bytes=32 * 1024)
    target_date = str(payload.get("target_date", "")).strip()
    release_ids = payload.get("release_ids")
    if not isinstance(release_ids, list):
        abort(400, description="DEVICE-008 release_ids 必須是陣列")
    try:
        return _offline_schedules().prepare_day(
            device_id=device_id,
            target_date=target_date,
            release_ids=[str(value) for value in release_ids],
        ), 201
    except KeyError:
        abort(404, description="DEVICE-002 找不到或停用的裝置")
    except ValueError as exc:
        abort(400, description=str(exc))


@bp.post("/api/v1/devices/<device_id>/offline-schedule/<schedule_id>/slots/<int:slot_index>")
@administrator_required
def replace_offline_schedule_slot(device_id: str, schedule_id: str, slot_index: int):
    payload = _json_payload("DEVICE-008", maximum_bytes=32 * 1024)
    release_id = str(payload.get("release_id", "")).strip()
    expected = payload.get("expected_config_version")
    try:
        expected_version = None if expected is None else int(expected)
    except (TypeError, ValueError):
        abort(400, description="DEVICE-008 expected_config_version 必須是整數")
    try:
        return _offline_schedules().replace_slot(
            device_id=device_id,
            schedule_id=schedule_id,
            slot_index=slot_index,
            release_id=release_id,
            expected_config_version=expected_version,
        )
    except KeyError:
        abort(404, description="DEVICE-002 找不到或停用的離線排程")
    except IndexError:
        abort(404, description="DEVICE-008 找不到指定 Slot")
    except ValueError as exc:
        abort(409, description=str(exc))


@bp.get("/api/device/v1/offline-schedule")
def device_offline_schedule():
    device = authenticate_device_request()
    if str(device["delivery_mode"] or "legacy_online") != "inktime_offline_schedule":
        abort(409, description="DEVICE-008 裝置目前不是 enhanced offline schedule 模式")
    if not offline_schedule_capability_is_usable(device["offline_schedule_capability_state"]):
        abort(409, description="DEVICE-008 裝置離線 Slot 能力尚未確認，暫停離線排程傳送")
    requested_targets = request.args.getlist("target")
    if len(requested_targets) > 1:
        abort(400, description="DEVICE-008 target 只允許單一 current 或 next")
    target = (requested_targets[0] if requested_targets else "current").strip().lower()
    if target not in {"current", "next"}:
        abort(400, description="DEVICE-008 target 只允許 current 或 next")
    try:
        local_zone = ZoneInfo(str(device["timezone"]))
        current_day = datetime.now(local_zone).date()
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        abort(409, description="DEVICE-008 裝置 IANA 時區不合法")
    target_day = current_day if target == "current" else current_day + timedelta(days=1)
    target_date = target_day.isoformat()
    result = _offline_schedules().ready_for_device(
        device_id=str(device["id"]),
        target_date=target_date,
        config_version=int(device["config_version"]),
    )
    if result is None:
        now = datetime.now(timezone.utc)
        now_epoch = int(now.timestamp())
        try:
            device_schedule = json.loads(str(device["schedule_times_json"] or "[]"))
            server_margin = int(
                current_app.extensions["inktime_settings_repository"].get(
                    "offline.server_prefetch_margin_minutes", 15
                )
            )
            if target == "next":
                retry_details = OfflineScheduleRepository.retry_after_target_details(
                    now=now,
                    timezone_name=str(device["timezone"]),
                    target_date=target_date,
                    schedule_times=device_schedule,
                    prefetch_lead_minutes=int(device["prefetch_lead_minutes"]),
                    server_margin_minutes=max(0, min(server_margin, 60)),
                    sync_strategy=str(device["sync_strategy"] or "first_display_lead"),
                    sync_time=device["sync_time"],
                    minimum_gap_minutes=int(
                        device["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
                    ),
                    maximum_slots=resolve_offline_schedule_max_slots(
                        {"offline_schedule_max_slots": device["offline_schedule_max_slots"]}
                    ),
                )
            else:
                retry_details = OfflineScheduleRepository.retry_after_details(
                    now=now,
                    timezone_name=str(device["timezone"]),
                    schedule_times=device_schedule,
                    prefetch_lead_minutes=int(device["prefetch_lead_minutes"]),
                    server_margin_minutes=max(0, min(server_margin, 60)),
                    sync_strategy=str(device["sync_strategy"] or "first_display_lead"),
                    sync_time=device["sync_time"],
                    minimum_gap_minutes=int(
                        device["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
                    ),
                    maximum_slots=resolve_offline_schedule_max_slots(
                        {"offline_schedule_max_slots": device["offline_schedule_max_slots"]}
                    ),
                )
            retry_after_epoch = retry_details.retry_after_epoch
            next_slot_epoch = retry_details.next_slot_epoch
        except (TypeError, ValueError, json.JSONDecodeError, KeyError):
            # A malformed live device setting is itself bounded recovery
            # input; never make the firmware poll at a fixed one-minute rate.
            retry_after_epoch = now_epoch + 15 * 60
            next_slot_epoch = None
        response = jsonify(
            {
                "error": "schedule_not_ready",
                "error_code": "DEVICE-008",
                "message": "schedule_not_ready",
                "target": target,
                "target_date": target_date,
                "retry_after_epoch": int(retry_after_epoch),
                "next_slot_epoch": next_slot_epoch,
            }
        )
        response.status_code = 404
        response.headers["Retry-After"] = str(max(1, int(retry_after_epoch) - now_epoch))
        return response
    try:
        schedule_times = json.loads(str(result["schedule"]["schedule_times_json"] or "[]"))
        minimum_gap_minutes = int(
            result["schedule"]["minimum_schedule_gap_minutes"] or MINIMUM_SCHEDULE_GAP_MINUTES
        )
        schedule_times = validate_offline_schedule(
            schedule_times,
            maximum=resolve_offline_schedule_max_slots(
                {"offline_schedule_max_slots": device["offline_schedule_max_slots"]}
            ),
            minimum_gap_minutes=minimum_gap_minutes,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        abort(409, description="DEVICE-008 離線排程快照 schedule_times 不可解析")
    schedule = result["schedule"]
    timezone_name = str(schedule["timezone"])
    try:
        snapshot_zone = ZoneInfo(timezone_name)
        target_day = date.fromisoformat(str(schedule["target_date"]))
        target_start = datetime.combine(target_day, time.min, tzinfo=snapshot_zone)
        target_end = datetime.combine(target_day + timedelta(days=1), time.min, tzinfo=snapshot_zone)
        target_start_epoch = int(target_start.astimezone(timezone.utc).timestamp())
        target_end_epoch = int(target_end.astimezone(timezone.utc).timestamp())
        utc_offset = target_start.utcoffset()
        utc_offset_minutes = int(utc_offset.total_seconds() // 60) if utc_offset is not None else None
        slots = []
        for raw_slot in result["slots"]:
            slot = dict(raw_slot)
            slot["show_at"] = datetime.fromisoformat(str(slot["show_at"])).astimezone(snapshot_zone).isoformat()
            slot["show_at_epoch"] = int(datetime.fromisoformat(str(slot["show_at"])).timestamp())
            slots.append(slot)
    except (TypeError, ValueError, OverflowError, ZoneInfoNotFoundError):
        abort(409, description="DEVICE-008 離線排程時間資料不合法")
    device_projection = result.get("device") or {}
    panel_profile = device_projection.get("panel_profile")
    rotation = device_projection.get("rotation")
    prefetch_lead = device_projection.get("prefetch_lead_minutes")
    schedule_version = device_projection.get("offline_schedule_version")
    sync_strategy = device_projection.get("sync_strategy")
    sync_time = device_projection.get("sync_time")
    if (
        panel_profile is None
        or rotation is None
        or prefetch_lead is None
        or schedule_version is None
        or sync_strategy is None
    ):
        abort(409, description="DEVICE-008 離線排程快照不完整")
    button_wake_action = device_projection.get("button_wake_action")
    if button_wake_action is None:
        abort(409, description="DEVICE-008 離線排程快照 button_wake_action 不完整")
    next_target_day = target_day + timedelta(days=1)
    next_target_start = datetime.combine(next_target_day, time.min, tzinfo=snapshot_zone)
    next_target_start_epoch = int(next_target_start.astimezone(timezone.utc).timestamp())
    first_next_slot = OfflineScheduleRepository._show_at(
        next_target_day, schedule_times[0], timezone_name
    )
    next_first_slot_epoch = int(datetime.fromisoformat(first_next_slot).timestamp())
    try:
        normalized_sync_strategy, normalized_sync_time = normalize_sync_strategy(
            str(sync_strategy), sync_time
        )
    except ValueError:
        abort(409, description="DEVICE-008 離線排程快照同步策略不完整")
    if normalized_sync_strategy == "fixed_daily":
        assert normalized_sync_time is not None
        sync_hour, sync_minute = (int(part) for part in normalized_sync_time.split(":"))
        next_prefetch_epoch = int(
            datetime.combine(
                next_target_day, time(sync_hour, sync_minute), tzinfo=snapshot_zone
            )
            .astimezone(timezone.utc)
            .timestamp()
        )
    else:
        next_prefetch_epoch = next_first_slot_epoch - int(prefetch_lead) * 60
    if (
        next_prefetch_epoch <= int(datetime.now(timezone.utc).timestamp())
        or next_prefetch_epoch >= next_first_slot_epoch
    ):
        # A past/invalid technical deadline is not a wake instruction.  Keep
        # the field present so firmware can fail closed without guessing.
        next_prefetch_epoch = 0
    queue_version = max((int(slot.get("queue_version") or 0) for slot in slots), default=0)
    snapshot_json = device_projection.get("snapshot_json")
    playlist_version = str(
        result.get("playlist_version")
        or (snapshot_json.get("playlist_version") if isinstance(snapshot_json, dict) else "")
        or ""
    )
    return {
        "schema_version": 1,
        "device_id": str(device["id"]),
        "schedule_id": str(schedule["id"]),
        "config_version": int(schedule["config_version"]),
        "target": target,
        "target_date": str(schedule["target_date"]),
        "target_local_date": str(schedule["target_date"]),
        "timezone": str(schedule["timezone"]),
        "target_start_epoch": target_start_epoch,
        "target_end_epoch": target_end_epoch,
        "next_target_start_epoch": next_target_start_epoch,
        "next_schedule_prefetch_epoch": next_prefetch_epoch,
        "utc_offset_minutes_for_target_date": utc_offset_minutes,
        "delivery_mode": "inktime_offline_schedule",
        "panel_profile": str(panel_profile),
        "rotation": int(rotation),
        "schedule": schedule_times,
        "schedule_times": schedule_times,
        "prefetch_lead_minutes": int(prefetch_lead),
        "button_wake_action": str(button_wake_action),
        "offline_schedule_version": int(schedule_version),
        "minimum_schedule_gap_minutes": minimum_gap_minutes,
        "sync_strategy": normalized_sync_strategy,
        "sync_time": normalized_sync_time,
        "queue_version": queue_version,
        "playlist_version": playlist_version,
        "status": str(schedule["status"]),
        "slots": slots,
    }


@bp.get("/api/device/v1/releases/<release_id>/files/<path:filename>")
def release_file(release_id: str, filename: str):
    device = authenticate_device_request()
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
    device = authenticate_device_request()
    payload = _json_payload("DEVICE-004")

    def optional_int(key: str, minimum: int, maximum: int) -> int | None:
        try:
            return optional_json_int(
                payload,
                key,
                minimum=minimum,
                maximum=maximum,
                error_prefix="DEVICE-004",
            )
        except JsonScalarError as exc:
            abort(400, description=str(exc))

    def optional_float(key: str, minimum: float, maximum: float) -> float | None:
        try:
            return optional_json_float(
                payload,
                key,
                minimum=minimum,
                maximum=maximum,
                error_prefix="DEVICE-004",
            )
        except JsonScalarError as exc:
            abort(400, description=str(exc))

    def optional_text(key: str, maximum: int) -> str | None:
        if key not in payload:
            return None
        value = payload[key]
        if type(value) is not str:
            abort(400, description=f"DEVICE-004 {key} 必須是字串")
        value = value.strip()
        if len(value) > maximum:
            abort(400, description=f"DEVICE-004 {key} 過長")
        return value

    def nullable_int(key: str, minimum: int, maximum: int) -> int | None:
        try:
            return nullable_json_int(
                payload,
                key,
                minimum=minimum,
                maximum=maximum,
                error_prefix="DEVICE-004",
            )
        except JsonScalarError as exc:
            abort(400, description=str(exc))

    battery_percent = optional_float("battery_percent", 0.0, 100.0)
    error_code = str(payload.get("error_code", "")).strip()[:64]
    error_message = str(payload.get("error_message", "")).strip()[:500]
    display_updated = optional_bool(payload, "display_updated", default=False)
    display_skipped = optional_bool(payload, "display_skipped", default=False)
    payload_verified = optional_bool(payload, "payload_sha256_verified", default=False)
    assert display_updated is not None
    assert display_skipped is not None
    assert payload_verified is not None
    display_skip_reason = str(payload.get("display_skip_reason", "")).strip()
    if display_skipped and display_skip_reason != "same_sha256":
        abort(400, description="DEVICE-004 display_skip_reason 必須是 same_sha256")
    if not display_skipped and display_skip_reason:
        abort(400, description="DEVICE-004 未 skip 時不得提供 display_skip_reason")
    if len(display_skip_reason) > 64:
        abort(400, description="DEVICE-004 display_skip_reason 過長")
    applied_offline_schedule_version = nullable_int(
        "applied_offline_schedule_version", 0, 2_147_483_647
    )
    telemetry = {
        "wifi_connect_ms": optional_int("wifi_connect_ms", 0, 120_000),
        "network_session_ms": optional_int("network_session_ms", 0, 600_000),
        "http_request_count": optional_int("http_request_count", 0, 128),
        "tls_handshake_count": optional_int("tls_handshake_count", 0, 128),
        "ntp_sync_ms": optional_int("ntp_sync_ms", 0, 120_000),
        "download_bytes": optional_int("download_bytes", 0, 4_294_967_295),
        "sd_read_bytes": optional_int("sd_read_bytes", 0, 4_294_967_295),
        "sd_write_bytes": optional_int("sd_write_bytes", 0, 4_294_967_295),
        "sd_write_ms": optional_int("sd_write_ms", 0, 600_000),
        "nvs_write_count": optional_int("nvs_write_count", 0, 1_024),
        "ack_event_count": optional_int("ack_event_count", 0, 1_024),
        "ack_batch_request_count": optional_int("ack_batch_request_count", 0, 1_024),
        "i2c_retry_count": optional_int("i2c_retry_count", 0, 4_294_967_295),
        "i2c_bus_reset_count": optional_int("i2c_bus_reset_count", 0, 4_294_967_295),
        "i2c_fail_closed_count": optional_int("i2c_fail_closed_count", 0, 4_294_967_295),
        "gc_deleted_files": optional_int("gc_deleted_files", 0, 4_294_967_295),
        "gc_deleted_bytes": optional_int("gc_deleted_bytes", 0, 4_294_967_295),
        "gc_skipped_protected": optional_int("gc_skipped_protected", 0, 4_294_967_295),
        "epd_transfer_ms": optional_int("epd_transfer_ms", 0, 600_000),
        "next_wake_epoch": nullable_int("next_wake_epoch", 0, 4_294_967_295),
        "next_network_sync_epoch": nullable_int(
            "next_network_sync_epoch", 0, 4_294_967_295
        ),
    }
    tls_handshake_count_unavailable = optional_bool(
        payload, "tls_handshake_count_unavailable"
    )
    tls_handshake_count_unavailable_reason = optional_text(
        "tls_handshake_count_unavailable_reason", 64
    )
    if (
        tls_handshake_count_unavailable is True
        and not tls_handshake_count_unavailable_reason
    ):
        abort(
            400,
            description=(
                "DEVICE-004 tls_handshake_count_unavailable_reason "
                "不可為空"
            ),
        )
    if (
        tls_handshake_count_unavailable is False
        and tls_handshake_count_unavailable_reason
    ):
        abort(
            400,
            description=(
                "DEVICE-004 tls_handshake_count_unavailable_reason "
                "僅可在 handshake count unavailable 時提供"
            ),
        )
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
            "wifi_fast_path_attempted",
            "wifi_fast_path_success",
            "ntp_sync_attempted",
            "ntp_sync_succeeded",
        )
    }
    wake_reason_detail = optional_text("wake_reason_detail", 64)
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
        applied_offline_schedule_version=applied_offline_schedule_version,
        details={
            "display_updated": display_updated,
            "display_skipped": display_skipped,
            "display_skip_reason": display_skip_reason,
            "payload_sha256_verified": payload_verified,
            "release_id": str(payload.get("release_id", ""))[:100],
            "render_profile": str(payload.get("render_profile", ""))[:100],
            "reported_panel_profile": str(payload.get("panel_profile", ""))[:100],
            "applied_config_version": payload.get("applied_config_version"),
            "applied_offline_schedule_version": applied_offline_schedule_version,
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
            "wifi_connect_ms": telemetry["wifi_connect_ms"],
            "wifi_fast_path_attempted": boolean_details["wifi_fast_path_attempted"],
            "wifi_fast_path_success": boolean_details["wifi_fast_path_success"],
            "network_session_ms": telemetry["network_session_ms"],
            "http_request_count": telemetry["http_request_count"],
            "tls_handshake_count": telemetry["tls_handshake_count"],
            "tls_handshake_count_unavailable": tls_handshake_count_unavailable,
            "tls_handshake_count_unavailable_reason": (
                tls_handshake_count_unavailable_reason
            ),
            "ntp_sync_attempted": boolean_details["ntp_sync_attempted"],
            "ntp_sync_succeeded": boolean_details["ntp_sync_succeeded"],
            "ntp_sync_ms": telemetry["ntp_sync_ms"],
            "download_bytes": telemetry["download_bytes"],
            "sd_read_bytes": telemetry["sd_read_bytes"],
            "sd_write_bytes": telemetry["sd_write_bytes"],
            "sd_write_ms": telemetry["sd_write_ms"],
            "nvs_write_count": telemetry["nvs_write_count"],
            "ack_event_count": telemetry["ack_event_count"],
            "ack_batch_request_count": telemetry["ack_batch_request_count"],
            "i2c_retry_count": telemetry["i2c_retry_count"],
            "i2c_bus_reset_count": telemetry["i2c_bus_reset_count"],
            "i2c_fail_closed_count": telemetry["i2c_fail_closed_count"],
            "gc_deleted_files": telemetry["gc_deleted_files"],
            "gc_deleted_bytes": telemetry["gc_deleted_bytes"],
            "gc_skipped_protected": telemetry["gc_skipped_protected"],
            "epd_transfer_ms": telemetry["epd_transfer_ms"],
            "next_wake_epoch": telemetry["next_wake_epoch"],
            "next_network_sync_epoch": telemetry["next_network_sync_epoch"],
            "wake_reason_detail": wake_reason_detail,
        },
    )
    DeviceTestReleaseStore(current_app.config["INKTIME_RELEASE_DIR"]).confirm_display(
        str(device["id"]),
        str(payload.get("release_id", ""))[:100],
        profile_key=str(device["panel_profile"]),
        payload_verified=payload_verified,
        display_updated=display_updated or display_skipped,
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
