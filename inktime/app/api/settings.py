from __future__ import annotations

from datetime import timedelta
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, cast

from flask import Blueprint, abort, current_app, g, make_response, render_template, request

from inktime.app.core.json_values import (
    JsonScalarError,
    json_int,
    json_object_payload,
    json_float,
    json_bool,
    nullable_json_int,
    nullable_json_float,
    reject_unknown_fields,
)
from inktime.app.core.logging import configure_logging
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.providers.config import (
    PROVIDER_KINDS,
    capabilities_for,
    effective_provider_kind,
    normalize_options,
    validate_base_url,
)
from inktime.app.repositories.settings import (
    RANKING_WEIGHT_KEYS,
    SENSITIVE_STATUS_KEYS,
    SETTINGS_SCHEMA_VERSION,
    SETTING_DEFINITIONS,
)
from inktime.app.web.access import administrator_required, login_required
from inktime.app.domain.rendering.system_presets import SYSTEM_PRESETS
from inktime.app.services.provider_contracts import run_provider_contract

bp = Blueprint("settings", __name__)


def _payload(error_prefix: str = "SET-002", *, maximum_bytes: int = 256 * 1024) -> dict:
    return json_object_payload(request, maximum_bytes=maximum_bytes, error_prefix=error_prefix)


@bp.get("/api/v1/settings/presets")
@login_required
def list_presets():
    return {"presets": list(SYSTEM_PRESETS.values())}


@bp.post("/api/v1/settings/presets/<key>/preview")
@administrator_required
def preview_preset(key: str):
    preset = SYSTEM_PRESETS.get(key)
    if preset is None:
        return {"error_code": "PRESET-001", "message": "找不到 Preset"}, 404
    preset_settings = cast(dict[str, Any], preset["settings"])
    repository = current_app.extensions["inktime_settings_repository"]
    changed, current, _merged = repository.prepare_updates(preset_settings, reject_control_center=True)
    compatible_profiles = set(cast(list[str], preset["compatible_panel_profiles"]))
    with current_app.extensions["inktime_database"].session() as connection:
        devices = connection.execute(
            "SELECT id,name,panel_profile FROM devices WHERE enabled=1 ORDER BY name,id"
        ).fetchall()
    compatible = [dict(row) for row in devices if str(row["panel_profile"]) in compatible_profiles]
    incompatible = [dict(row) for row in devices if str(row["panel_profile"]) not in compatible_profiles]
    return {
        "preset": preset,
        "changed_keys": list(changed),
        "unchanged_keys": [name for name in preset_settings if name not in changed],
        "affected_devices": compatible,
        "incompatible_devices": incompatible,
        "requires_new_release": bool(changed),
        "current": {name: current[name] for name in preset_settings},
    }


@bp.post("/api/v1/settings/presets/<key>/apply")
@administrator_required
def apply_preset(key: str):
    preset = SYSTEM_PRESETS.get(key)
    if preset is None:
        return {"error_code": "PRESET-001", "message": "找不到 Preset"}, 404
    preset_settings = cast(dict[str, Any], preset["settings"])
    payload = _payload("PRESET-002")
    selected = payload.get("update_existing_device_ids", [])
    if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
        abort(400, description="PRESET-002 update_existing_device_ids 必須是裝置 ID 陣列")
    if selected and payload.get("confirm_physical_panel") is not True:
        abort(409, description="PRESET-003 更新既有裝置需要 confirm_physical_panel=true")
    compatible_profiles = set(cast(list[str], preset["compatible_panel_profiles"]))
    try:
        result = current_app.extensions["inktime_settings_mutation_service"].apply_preset_atomic(
            preset_settings,
            device_ids=selected,
            compatible_panel_profiles=compatible_profiles,
            target_panel_profile=str(preset_settings["device.default_panel_profile"]),
            changed_by=str(g.user["id"]),
            source_ip=request.remote_addr or "unknown",
            reason=f"preset:{key}",
        )
    except ValueError as exc:
        abort(409, description=str(exc))
    changed = result["changed_keys"]
    return result | {
        "preset": key,
        "changed_keys": changed,
        "unchanged_keys": [name for name in preset_settings if name not in changed],
        "incompatible_devices": [],
        "requires_new_release": bool(changed or result["changed_device_count"]),
    }


@bp.get("/settings")
@login_required
def settings_page():
    is_administrator = g.user["role"] == "administrator"
    rows = [
        row
        for row in current_app.extensions["inktime_settings_repository"].all(
            redact_sensitive=not is_administrator
        )
        if row["definition"] and not row["definition"].get("control_center")
    ]
    categories = {}
    for row in rows:
        categories.setdefault(row["category"], []).append(row)
    with current_app.extensions["inktime_database"].session() as connection:
        feature_flags = connection.execute("SELECT * FROM feature_flags ORDER BY key").fetchall()
    return render_template(
        "settings.html",
        categories=categories,
        setting_count=len(rows),
        feature_flags=feature_flags,
        history=current_app.extensions["inktime_settings_repository"].history(
            100, redact_source_ip=not is_administrator
        ),
        snapshots=current_app.extensions["inktime_settings_repository"].snapshots(30),
        deployment={
            "docker": Path("/.dockerenv").exists(),
            "port": os.environ.get("INKTIME_PORT", "8765"),
            "data_dir": os.environ.get("INKTIME_DATA_DIR", "data"),
            "photo_dir": os.environ.get("INKTIME_PHOTO_DIR", "未設定"),
            "access_log": os.environ.get("INKTIME_ACCESS_LOG", "0") == "1",
            "revision": os.environ.get("INKTIME_GIT_REVISION", "unknown"),
        },
        webhook_token_configured=current_app.extensions["inktime_notification_service"].token_configured(),
    )


@bp.post("/api/v1/settings")
@administrator_required
def update_settings():
    payload = _payload()
    repository = current_app.extensions["inktime_settings_repository"]
    try:
        changed, current, _merged = repository.prepare_updates(payload, reject_control_center=True)
        impact = _impact(changed)
        high_risk = _confirmation_reasons(changed, current, impact)
        if high_risk and request.headers.get("X-InkTime-Confirm-Risk") != "true":
            abort(
                409,
                description="SET-007 高風險變更需要先預覽並明確確認：" + "、".join(high_risk),
            )
        result = current_app.extensions["inktime_settings_mutation_service"].update_many(
            payload,
            changed_by=g.user["id"],
            source_ip=request.remote_addr or "unknown",
            reason=request.headers.get("X-InkTime-Change-Reason"),
            reject_control_center=True,
        )
    except PermissionError as exc:
        abort(400, description=f"SET-001 {_permission_error_message(exc)}")
    except KeyError as exc:
        abort(400, description=f"SET-001 未知設定：{exc.args[0]}")
    except (TypeError, ValueError) as exc:
        abort(400, description=f"SET-002 {exc}")
    configure_logging(settings_repository=repository)
    current_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=int(repository.get("security.session_minutes", 30))
    )
    return {"status": "ok"} | result


def _bounded_counts() -> dict[str, int]:
    database = current_app.extensions["inktime_database"]
    queries = {
        "affected_device_count": "SELECT COUNT(*) FROM devices WHERE enabled=1",
        "affected_photo_estimate": "SELECT COUNT(*) FROM photos",
        "affected_release_count": "SELECT COUNT(*) FROM releases",
    }
    result: dict[str, int] = {}
    with database.session() as connection:
        for key, sql in queries.items():
            try:
                row = connection.execute(sql).fetchone()
                result[key] = min(int(row[0]) if row else 0, 10_000_000)
            except Exception:
                result[key] = 0
    return result


def _impact(changed: dict[str, object]) -> dict[str, object]:
    known = {key: value for key, value in changed.items() if key in SETTING_DEFINITIONS}
    definitions = [SETTING_DEFINITIONS[key] for key in known]
    changed_keys = sorted(known)
    cache_changed = any(bool(definition["cache_impact"]) for definition in definitions)
    reanalysis = any(bool(definition["reanalysis_impact"]) for definition in definitions)
    rerender = any(bool(definition["rerender_impact"]) for definition in definitions)
    ranking_only = bool(known) and set(known).issubset(set(RANKING_WEIGHT_KEYS))
    counts = _bounded_counts()
    warnings = [
        f"{SETTING_DEFINITIONS[key]['label_zh_tw']}目前尚未接上 runtime 動態讀取"
        for key in known
        if not SETTING_DEFINITIONS[key]["runtime_wired"]
    ]
    if "analysis.ai_mode" in known and known["analysis.ai_mode"] == "full_library":
        warnings.append("完整照片庫 AI 分析屬高風險變更；套用前需再次確認")
    if any(
        key in known
        for key in (
            "budget.daily_stop",
            "budget.monthly_stop",
            "analysis.ai_daily_photo_limit",
            "analysis.ai_monthly_photo_limit",
        )
    ):
        warnings.append("提高預算或照片上限可能增加 Provider 成本")
    estimated_requests = counts["affected_photo_estimate"] if reanalysis else 0
    return {
        "changed_keys": changed_keys,
        "validation_errors": [],
        "warnings": warnings,
        "restart_required": any(bool(definition["restart_required"]) for definition in definitions),
        "affects_new_jobs": any(definition["effective_scope"] == "next_job" for definition in definitions),
        "cache_fingerprint_changed": cache_changed,
        "ranking_only": ranking_only,
        "reanalysis_required": reanalysis and not ranking_only,
        "rerender_required": rerender,
        **counts,
        "estimated_ai_requests": estimated_requests,
        "estimated_cost": None,
        "estimated_cost_label": "無法估算",
        "unsupported_impacts": [
            "自動重跑過期照片",
            "裝置群組覆寫",
            "可靠 Provider 成本估算",
        ],
        "existing_releases_unchanged": any(bool(definition["rerender_impact"]) for definition in definitions),
        "release_notice": (
            "既有 Release 不會因設定修改而改變；必須建立新 Release 才會套用新渲染設定" if rerender else None
        ),
    }


def _permission_error_message(exc: PermissionError) -> str:
    key = str(exc)
    if "尚未接上 Runtime" in key:
        return key
    return f"請從評分控制中心修改：{key}"


def _confirmation_reasons(
    changed: dict[str, object],
    current: dict[str, object],
    impact: dict[str, object],
) -> list[str]:
    reasons = [
        f"{SETTING_DEFINITIONS[key]['label_zh_tw']}屬高風險設定"
        for key in changed
        if SETTING_DEFINITIONS[key]["risk"] == "high"
    ]
    if changed.get("analysis.ai_mode") == "full_library":
        reasons.append("完整照片庫 AI 分析")
    for key in (
        "analysis.ai_daily_photo_limit",
        "analysis.ai_monthly_photo_limit",
        "budget.daily_stop",
        "budget.monthly_stop",
    ):
        if key in changed and float(str(changed[key])) > max(
            float(str(current[key])) * 2, float(str(current[key])) + 1
        ):
            reasons.append(str(SETTING_DEFINITIONS[key]["label_zh_tw"]))
    if impact["cache_fingerprint_changed"]:
        reasons.append("改變 AI Cache Fingerprint")
    if "observability.activity_retention_days" in changed and int(
        str(changed["observability.activity_retention_days"])
    ) < int(str(current["observability.activity_retention_days"])):
        reasons.append("縮短重要 Activity 保留期間")
    if int(str(impact["affected_device_count"])) >= 10 and any(
        SETTING_DEFINITIONS[key]["device_override_allowed"] for key in changed
    ):
        reasons.append("影響大量裝置的系統預設")
    if int(str(impact["affected_release_count"])) >= 50 and impact["rerender_required"]:
        reasons.append("影響大量既有 Release 的渲染判讀")
    return list(dict.fromkeys(reasons))


@bp.post("/api/v1/settings/preview")
@login_required
def preview_settings():
    payload = _payload()
    repository = current_app.extensions["inktime_settings_repository"]
    try:
        changed, _current, _merged = repository.prepare_updates(payload, reject_control_center=True)
    except PermissionError as exc:
        return _impact({}) | {
            "validation_errors": [_permission_error_message(exc)],
            "valid": False,
        }
    except KeyError as exc:
        return _impact({}) | {
            "validation_errors": [f"未知設定：{exc.args[0]}"],
            "valid": False,
        }
    except (TypeError, ValueError) as exc:
        return _impact({}) | {"validation_errors": [str(exc)], "valid": False}
    impact = _impact(changed)
    reasons = _confirmation_reasons(changed, _current, impact)
    return impact | {
        "valid": True,
        "normalized_changes": changed,
        "requires_confirmation": bool(reasons),
        "confirmation_reasons": reasons,
    }


@bp.get("/api/v1/settings/snapshots")
@login_required
def list_setting_snapshots():
    return {"snapshots": current_app.extensions["inktime_settings_repository"].snapshots(100)}


@bp.get("/api/v1/settings/metadata")
@login_required
def setting_metadata():
    repository = current_app.extensions["inktime_settings_repository"]
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "settings": [
            repository.public_metadata(key)
            for key, definition in SETTING_DEFINITIONS.items()
            if not definition.get("control_center")
        ],
    }


@bp.get("/api/v1/settings/snapshots/<snapshot_id>")
@login_required
def setting_snapshot(snapshot_id: str):
    try:
        snapshot = current_app.extensions["inktime_settings_repository"].snapshot(snapshot_id)
    except KeyError:
        abort(404, description="SET-004 找不到設定 Snapshot")
    changed_values = {
        str(item["key"]): item["new_value"]
        for item in snapshot["items"]
        if str(item["key"]) in SETTING_DEFINITIONS
    }
    return snapshot | {"impact": _impact(changed_values)}


@bp.post("/api/v1/settings/snapshots/<snapshot_id>/rollback-preview")
@administrator_required
def rollback_preview(snapshot_id: str):
    try:
        preview = current_app.extensions["inktime_settings_repository"].rollback_preview(snapshot_id)
    except KeyError:
        abort(404, description="SET-004 找不到設定 Snapshot")
    except ValueError as exc:
        abort(400, description=f"SET-002 {exc}")
    return preview | {"impact": _impact(preview["updates"])}


@bp.post("/api/v1/settings/snapshots/<snapshot_id>/rollback")
@administrator_required
def rollback_settings(snapshot_id: str):
    payload = _payload("SET-005")
    if payload.get("confirm") is not True:
        abort(400, description="SET-005 Rollback 需要明確確認")
    try:
        result = current_app.extensions["inktime_settings_mutation_service"].rollback(
            snapshot_id,
            changed_by=g.user["id"],
            source_ip=request.remote_addr or "unknown",
            reason=str(payload.get("reason") or "") or None,
        )
    except KeyError:
        abort(404, description="SET-004 找不到設定 Snapshot")
    except ValueError as exc:
        abort(400, description=f"SET-002 {exc}")
    return {"status": "ok"} | result


def _export_document() -> dict[str, object]:
    repository = current_app.extensions["inktime_settings_repository"]
    rows = repository.all()
    settings: dict[str, object] = {}
    sensitive_status: dict[str, object] = {}
    for row in rows:
        key = str(row["key"])
        if key not in SETTING_DEFINITIONS:
            continue
        definition = SETTING_DEFINITIONS[key]
        if definition.get("control_center"):
            continue
        if not definition.get("export_allowed", True):
            sensitive_status[key] = {"configured": row["value"] not in {None, ""}}
            continue
        settings[key] = row["value"]
    return {
        "format": "inktime-settings",
        "version": SETTINGS_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "application_version": current_app.config["INKTIME_VERSION"],
        "settings": settings,
        "sensitive_status": sensitive_status,
    }


@bp.get("/api/v1/settings/export")
@administrator_required
def export_settings():
    response = make_response(
        json.dumps(
            _export_document(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=inktime-settings.json"
    response.headers["Cache-Control"] = "no-store"
    return response


def _protected_import_reason(key: str) -> str | None:
    if key in SENSITIVE_STATUS_KEYS:
        return "敏感位置、私人路徑或 Webhook URI 只能在本機個別設定，不接受一般匯入"
    if key in SETTING_DEFINITIONS and not SETTING_DEFINITIONS[key].get("runtime_wired", True):
        return "尚未接上 Runtime，僅供唯讀"
    if key in SETTING_DEFINITIONS and (
        SETTING_DEFINITIONS[key].get("control_center") or SETTING_DEFINITIONS[key].get("secret")
    ):
        return "必須由專屬控制中心或 Secret Store 管理"
    if key.startswith(
        (
            "docker.",
            "deployment.",
            "hardware.",
            "firmware.",
            "protocol.",
            "database.",
            "migration.",
            "secret.",
        )
    ):
        return "屬部署、資料庫、硬體或協議安全邊界"
    if key in {
        "security.auth_enabled",
        "security.csrf_enabled",
        "security.session_secret",
        "security.master_key",
    }:
        return "不得透過設定匯入關閉認證或變更安全密鑰"
    return None


def _import_preview(document: object) -> dict[str, object]:
    if not isinstance(document, dict):
        raise ValueError("匯入內容必須是 JSON 物件")
    if document.get("format") != "inktime-settings":
        raise ValueError("匯入格式必須是 inktime-settings")
    if document.get("version") != SETTINGS_SCHEMA_VERSION:
        raise ValueError(f"只支援設定格式版本 {SETTINGS_SCHEMA_VERSION}")
    raw_settings = document.get("settings")
    if not isinstance(raw_settings, dict):
        raise ValueError("settings 必須是 JSON 物件")
    protected_reasons = {
        str(key): reason for key in raw_settings if (reason := _protected_import_reason(str(key))) is not None
    }
    blocked_keys = sorted(protected_reasons)
    unknown_keys = sorted(set(map(str, raw_settings)) - set(SETTING_DEFINITIONS) - set(blocked_keys))
    accepted = {
        str(key): value
        for key, value in raw_settings.items()
        if str(key) in SETTING_DEFINITIONS and str(key) not in blocked_keys
    }
    repository = current_app.extensions["inktime_settings_repository"]
    changed, _current, _merged = repository.prepare_updates(accepted, reject_control_center=True)
    return {
        "valid": True,
        "unknown_keys": unknown_keys,
        "blocked_keys": blocked_keys,
        "blocked_reasons": protected_reasons,
        "skipped_keys": sorted(set(unknown_keys) | set(blocked_keys)),
        "changes": changed,
        "impact": _impact(changed),
    }


@bp.post("/api/v1/settings/import-preview")
@administrator_required
def import_preview():
    try:
        return _import_preview(_payload("SET-006", maximum_bytes=1024 * 1024))
    except (KeyError, PermissionError, TypeError, ValueError) as exc:
        abort(400, description=f"SET-006 {exc}")


@bp.post("/api/v1/settings/import")
@administrator_required
def import_settings():
    payload = _payload("SET-006", maximum_bytes=1024 * 1024)
    if payload.get("confirm") is not True:
        abort(400, description="SET-006 匯入需要明確確認")
    try:
        preview = _import_preview(payload.get("document"))
        result = current_app.extensions["inktime_settings_mutation_service"].update_many(
            preview["changes"],
            changed_by=g.user["id"],
            source_ip=request.remote_addr or "unknown",
            reason=str(payload.get("reason") or "匯入設定")[:500],
            reject_control_center=True,
        )
    except (KeyError, PermissionError, TypeError, ValueError) as exc:
        abort(400, description=f"SET-006 {exc}")
    return {"status": "ok", "preview": preview} | result


@bp.get("/providers")
@login_required
def providers_page():
    return render_template(
        "providers.html", providers=current_app.extensions["inktime_provider_repository"].list()
    )


@bp.post("/api/v1/providers")
@administrator_required
def save_provider():
    payload = _payload("SET-003")
    try:
        reject_unknown_fields(
            payload,
            {
                "id",
                "name",
                "kind",
                "base_url",
                "api_key",
                "enabled",
                "priority",
                "supports_vision",
                "supports_batch",
                "supports_json_schema",
                "rate_limit_rpm",
                "token_limit_tpm",
                "max_concurrency",
                "timeout_seconds",
                "cooldown_seconds",
                "options",
            },
            error_prefix="SET-003",
        )
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    for field in ("id", "name", "kind", "base_url", "api_key"):
        if field in payload and type(payload[field]) is not str:
            abort(400, description=f"SET-003 {field} 必須是字串")
    if not payload.get("base_url") or not payload.get("name"):
        abort(400, description="SET-003 Provider 名稱與 URL 不可空白")
    if type(payload["name"]) is not str or not payload["name"].strip() or len(payload["name"].strip()) > 120:
        abort(400, description="SET-003 Provider 名稱必須是 1 至 120 字元")
    requested_kind = (payload.get("kind") or "openai_compatible").strip().lower()
    if requested_kind not in PROVIDER_KINDS:
        abort(400, description=f"SET-003 不支援的 Provider kind：{requested_kind}")
    try:
        kind = effective_provider_kind(requested_kind, str(payload["base_url"]))
        options = normalize_options(kind, payload.get("options") or {})
        payload["options"] = options
        payload["kind"] = kind
        payload["base_url"] = validate_base_url(kind, str(payload["base_url"]), options)
        payload["enabled"] = json_bool(payload, "enabled", default=True, error_prefix="SET-003")
        payload["supports_vision"] = json_bool(payload, "supports_vision", default=True, error_prefix="SET-003")
        payload["supports_batch"] = json_bool(payload, "supports_batch", default=False, error_prefix="SET-003")
        payload["supports_json_schema"] = json_bool(
            payload, "supports_json_schema", default=True, error_prefix="SET-003"
        )
    except (ValueError, JsonScalarError) as exc:
        abort(400, description=f"SET-003 {exc}")
    try:
        for field, default, minimum, maximum in (
            ("priority", 100, 1, 10_000),
            ("max_concurrency", 2, 1, 32),
            ("timeout_seconds", 120, 5, 600),
            ("cooldown_seconds", 300, 1, 86_400),
        ):
            payload[field] = json_int(
                payload,
                field,
                default=default,
                minimum=minimum,
                maximum=maximum,
                error_prefix="SET-003",
            )
        for field in ("rate_limit_rpm", "token_limit_tpm"):
            payload[field] = nullable_json_int(
                payload,
                field,
                minimum=1,
                maximum=2_147_483_647,
                error_prefix="SET-003",
            )
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    try:
        provider_id = current_app.extensions["inktime_provider_repository"].save(payload, g.user["id"])
    except (ValueError, KeyError) as exc:
        abort(400, description=f"SET-003 {exc}")
    return {"id": provider_id}, 201


@bp.post("/api/v1/providers/<provider_id>/test")
@administrator_required
def test_provider(provider_id: str):
    payload = _payload("SET-005")
    try:
        reject_unknown_fields(payload, {"level"}, error_prefix="SET-005")
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    level = payload.get("level", 1)
    if type(level) is not int or level not in {1, 2, 3}:
        abort(400, description="SET-005 level 必須是 1、2 或 3")
    config = current_app.extensions["inktime_provider_repository"].get(provider_id, include_secret=True)
    if config is None:
        abort(404)
    kind = effective_provider_kind(
        str(config.get("kind") or "openai_compatible"), str(config.get("base_url") or "")
    )
    provider = OpenAICompatibleProvider(
        name=config["name"],
        base_url=config["base_url"],
        api_key=config.get("api_key", ""),
        kind=kind,
        provider_id=str(config["id"]),
        options=config.get("options") or {},
        pricing=current_app.extensions["inktime_provider_repository"].pricing(config["id"]),
        timeout=min(15, config["timeout_seconds"]),
        supports_json_schema=bool(config["supports_json_schema"]),
        supports_reasoning_effort=capabilities_for(
            kind, supports_json_schema=bool(config["supports_json_schema"])
        ).reasoning,
    )
    try:
        model = str(
            current_app.extensions["inktime_settings_repository"].get("model.analysis_model", "gpt-4o")
        )
        result = run_provider_contract(provider, level=level, model=model)
    except (ValueError, KeyError) as exc:
        abort(400, description=f"SET-005 {exc}")
    finally:
        provider.close()
    return result, 200 if result["ok"] else 502


@bp.post("/api/v1/providers/<provider_id>/pricing")
@administrator_required
def save_provider_pricing(provider_id: str):
    payload = _payload("SET-004")
    try:
        reject_unknown_fields(
            payload,
            {
                "model",
                "input_per_million",
                "cached_input_per_million",
                "output_per_million",
                "batch_multiplier",
                "batch_input_per_million",
                "batch_cached_input_per_million",
                "batch_output_per_million",
                "enabled",
            },
            error_prefix="SET-004",
        )
        if (
            type(payload.get("model")) is not str
            or not payload["model"].strip()
            or len(payload["model"].strip()) > 200
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in payload["model"])
        ):
            abort(400, description="SET-004 model 必須是 1 至 200 字元")
        payload["model"] = payload["model"].strip()
        for field in ("input_per_million", "cached_input_per_million", "output_per_million"):
            payload[field] = json_float(
                payload,
                field,
                minimum=0,
                maximum=1_000_000,
                error_prefix="SET-004",
            )
        payload["batch_multiplier"] = json_float(
            payload,
            "batch_multiplier",
            default=0.5,
            minimum=0,
            maximum=10,
            error_prefix="SET-004",
        )
        for field in (
            "batch_input_per_million",
            "batch_cached_input_per_million",
            "batch_output_per_million",
        ):
            payload[field] = nullable_json_float(
                payload,
                field,
                minimum=0,
                maximum=1_000_000,
                error_prefix="SET-004",
            )
        payload["enabled"] = json_bool(payload, "enabled", default=True, error_prefix="SET-004")
        reconciliation = current_app.extensions["inktime_provider_repository"].save_pricing(provider_id, payload)
    except (JsonScalarError, ValueError) as exc:
        abort(400, description=f"SET-004 {exc}")
    except KeyError:
        abort(404)
    return {"status": "ok", "provider_id": provider_id, "model": payload["model"], **reconciliation}


@bp.get("/costs")
@login_required
def costs_page():
    database = current_app.extensions["inktime_database"]
    with database.session() as connection:
        summary = connection.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN cost_source<>'unknown' AND date(started_at)=date('now') THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) today,
                   COALESCE(SUM(CASE WHEN cost_source<>'unknown' AND started_at>=datetime('now','-7 day') THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) week,
                   COALESCE(SUM(CASE WHEN cost_source<>'unknown' AND strftime('%Y-%m',started_at)=strftime('%Y-%m','now') THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END),0) month,
                   COALESCE(SUM(CASE WHEN cost_source='unknown' AND (
                       COALESCE(input_tokens,0)>0 OR COALESCE(output_tokens,0)>0 OR COALESCE(cached_tokens,0)>0
                       OR COALESCE(reasoning_tokens,0)>0 OR COALESCE(cache_write_tokens,0)>0
                       OR COALESCE(request_body_bytes,0)>0 OR COALESCE(image_bytes,0)>0
                       OR COALESCE(actual_cost,0)>0 OR COALESCE(estimated_cost,0)>0
                   ) THEN 1 ELSE 0 END),0) unknown_count,
                   COALESCE(SUM(CASE WHEN cost_source='unknown' AND date(started_at)=date('now') AND (
                       COALESCE(input_tokens,0)>0 OR COALESCE(output_tokens,0)>0 OR COALESCE(cached_tokens,0)>0
                       OR COALESCE(reasoning_tokens,0)>0 OR COALESCE(cache_write_tokens,0)>0
                       OR COALESCE(request_body_bytes,0)>0 OR COALESCE(image_bytes,0)>0
                       OR COALESCE(actual_cost,0)>0 OR COALESCE(estimated_cost,0)>0
                   ) THEN 1 ELSE 0 END),0) today_unknown_count,
                   COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens
            FROM api_usage
            """
        ).fetchone()
        by_model = connection.execute(
            """SELECT provider,model,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,
                      SUM(CASE WHEN cost_source<>'unknown' THEN COALESCE(actual_cost,estimated_cost) ELSE 0 END) cost,
                      SUM(CASE WHEN cost_source='unknown' AND (
                          COALESCE(input_tokens,0)>0 OR COALESCE(output_tokens,0)>0 OR COALESCE(cached_tokens,0)>0
                          OR COALESCE(reasoning_tokens,0)>0 OR COALESCE(cache_write_tokens,0)>0
                          OR COALESCE(request_body_bytes,0)>0 OR COALESCE(image_bytes,0)>0
                          OR COALESCE(actual_cost,0)>0 OR COALESCE(estimated_cost,0)>0
                      ) THEN 1 ELSE 0 END) unknown_count,
                      COUNT(*) requests
               FROM api_usage GROUP BY provider,model ORDER BY cost DESC"""
        ).fetchall()
        usage_retention = connection.execute(
            "SELECT retention_days FROM data_retention_policies WHERE data_type='api_usage'"
        ).fetchone()
    reserve = float(
        current_app.extensions["inktime_settings_repository"].get("budget.unknown_request_reserve", 0.25)
    )
    summary = dict(summary)
    summary["unknown_reserve"] = reserve
    summary["reserved_today"] = int(summary["today_unknown_count"]) * reserve
    summary["cost_complete"] = int(summary["unknown_count"]) == 0
    summary["usage_retention_days"] = (
        int(usage_retention["retention_days"]) if usage_retention is not None else None
    )
    return render_template("costs.html", summary=summary, by_model=by_model)
