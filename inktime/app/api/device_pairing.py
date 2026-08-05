from __future__ import annotations

from flask import Blueprint, abort, current_app, g, jsonify, request

from inktime.app.api.device_auth import authenticate_device_request
from inktime.app.core.json_values import JsonScalarError, json_object_payload
from inktime.app.services.device_pairing import DevicePairingError
from inktime.app.web.access import administrator_required


bp = Blueprint("pairing", __name__)
PAIRING_REQUEST_FIELDS = {
    "device_id",
    "pairing_nonce",
    "firmware_identity",
    "firmware_version",
    "panel_profile",
    "device_name",
    "capabilities",
}


@bp.after_request
def no_store_pairing_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _json_payload(*, maximum_bytes: int = 32 * 1024) -> dict:
    content_type = request.headers.get("Content-Type", "").strip().lower()
    if content_type != "application/json":
        abort(415, description="PAIR-004 Content-Type 必須是 application/json")
    try:
        payload = json_object_payload(
            request,
            maximum_bytes=maximum_bytes,
            error_prefix="PAIR-004",
        )
    except JsonScalarError as exc:
        abort(400, description=str(exc))
    return payload


def _strict(payload: dict, allowed: set[str]) -> None:
    unknown = set(payload) - allowed
    if unknown:
        abort(400, description="PAIR-004 JSON 含有不支援欄位")


def _https_policy() -> None:
    runtime_config = current_app.extensions.get("inktime_runtime_config")
    trusted_lan_http = runtime_config is not None \
        and bool(getattr(runtime_config, "allow_insecure_http", False)) \
        and str(getattr(runtime_config, "public_url", "")).startswith("http://")
    if runtime_config is not None and getattr(runtime_config, "environment", "") == "production" \
            and not request.is_secure and not trusted_lan_http:
        abort(400, description="PAIR-010 production pairing 必須使用 HTTPS")


def _result(payload: dict, status_code: int = 200, *, retry_after: int | None = None):
    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if retry_after is not None:
        response.headers["Retry-After"] = str(max(1, int(retry_after)))
    return response


def _service():
    return current_app.extensions["inktime_device_pairing_service"]


def _error(exc: DevicePairingError):
    return _result(
        {"error_code": exc.error_code, "message": str(exc)},
        exc.status_code,
        retry_after=exc.retry_after,
    )


@bp.post("/api/device/v1/pairing/request")
def request_pairing():
    _https_policy()
    payload = _json_payload()
    _strict(payload, PAIRING_REQUEST_FIELDS)
    try:
        status_code, result = _service().request_pairing(payload, ip_address=request.remote_addr or "unknown")
    except DevicePairingError as exc:
        return _error(exc)
    return _result(result, status_code)


@bp.post("/api/device/v1/pairing/claim")
def claim_pairing():
    _https_policy()
    payload = _json_payload()
    _strict(payload, {"pairing_id", "pairing_nonce"})
    try:
        status_code, result, retry_after = _service().claim(
            payload.get("pairing_id"),
            payload.get("pairing_nonce"),
        )
    except DevicePairingError as exc:
        return _error(exc)
    return _result(result, status_code, retry_after=retry_after)


@bp.post("/api/device/v1/pairing/confirm")
def confirm_pairing():
    _https_policy()
    payload = _json_payload(maximum_bytes=8 * 1024)
    _strict(payload, {"pairing_id", "device_id", "pairing_nonce"})
    authorization = request.headers.get("Authorization", "")
    if len(authorization) > 4096 or not authorization.startswith("Bearer "):
        abort(401, description="PAIR-008 confirm 必須使用 Device Secret Bearer")
    device_secret = authorization[7:].strip()
    raw_version = request.headers.get("X-InkTime-Credential-Version", "")
    if not device_secret or len(device_secret) > 1024 or not raw_version.isdigit() or len(raw_version) > 12:
        abort(401, description="PAIR-008 confirm credential header 不合法")
    try:
        status_code, result = _service().confirm(
            payload.get("pairing_id"),
            payload.get("device_id"),
            payload.get("pairing_nonce"),
            device_secret,
            int(raw_version),
        )
    except DevicePairingError as exc:
        return _error(exc)
    return _result(result, status_code)


@bp.get("/api/device/v1/pairing/repair-permission")
def repair_permission():
    _https_policy()
    authenticated = authenticate_device_request(
        repository=current_app.extensions["inktime_device_repository"],
        allow_repair=True,
    )
    return _result({"status": "pairing_allowed", "device_id": str(authenticated["id"])})


@bp.get("/api/v1/device-pairing/pending")
@administrator_required
def pending_pairings():
    return _result({"pairings": _service().pending_for_admin()})


@bp.post("/api/v1/device-pairing/<pairing_id>/approve")
@administrator_required
def approve_pairing(pairing_id: str):
    payload = _json_payload(maximum_bytes=8 * 1024)
    _strict(payload, {"pairing_code", "device_config"})
    device_config = payload.get("device_config", {})
    if not isinstance(device_config, dict):
        abort(400, description="PAIR-004 device_config 必須是 JSON object")
    user = getattr(g, "user", None)
    try:
        result = _service().approve(
            pairing_id,
            payload.get("pairing_code"),
            administrator_id=str(user["id"] if user is not None else "admin"),
            device_config=device_config,
        )
    except DevicePairingError as exc:
        return _error(exc)
    return _result(result)


@bp.post("/api/v1/device-pairing/<pairing_id>/reject")
@administrator_required
def reject_pairing(pairing_id: str):
    user = getattr(g, "user", None)
    try:
        _service().reject(
            pairing_id,
            administrator_id=str(user["id"] if user is not None else "admin"),
        )
    except KeyError:
        abort(404)
    except DevicePairingError as exc:
        return _error(exc)
    return _result({"status": "rejected", "pairing_id": pairing_id})


@bp.post("/api/v1/devices/<device_id>/revoke-credential")
@administrator_required
def revoke_credential(device_id: str):
    user = getattr(g, "user", None)
    try:
        _service().revoke(device_id, administrator_id=str(user["id"] if user is not None else "admin"))
    except KeyError:
        abort(404)
    except DevicePairingError as exc:
        return _error(exc)
    return _result({"status": "revoked", "device_id": device_id})


@bp.post("/api/v1/devices/<device_id>/enable-repair")
@administrator_required
def enable_repair(device_id: str):
    user = getattr(g, "user", None)
    try:
        _service().start_repair(device_id, administrator_id=str(user["id"] if user is not None else "admin"))
    except KeyError:
        abort(404)
    except DevicePairingError as exc:
        return _error(exc)
    return _result({"status": "pairing_pending", "device_id": device_id})
