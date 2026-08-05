from __future__ import annotations

from flask import current_app, g, request
from werkzeug.exceptions import TooManyRequests, Unauthorized

from inktime.app.repositories.devices import DeviceRateLimitError, DeviceRepository


def authenticate_device_request(
    *,
    repository: DeviceRepository | None = None,
    allow_repair: bool = False,
):
    """Authenticate every Device Bearer endpoint through one stable contract."""

    authorization = request.headers.get("Authorization", "")
    if len(authorization) > 4096 or not authorization.startswith("Bearer "):
        raise Unauthorized(description="DEVICE-001 裝置驗證失敗")
    token = authorization[7:].strip()
    if not token or len(token) > 1024:
        raise Unauthorized(description="DEVICE-001 裝置驗證失敗")
    raw_version = request.headers.get("X-InkTime-Credential-Version")
    credential_version = None
    if raw_version is not None:
        if len(raw_version) > 12 or not raw_version.isdigit():
            raise Unauthorized(description="DEVICE-001 裝置驗證失敗")
        credential_version = int(raw_version)
    selected_repository = repository or current_app.extensions["inktime_device_repository"]
    try:
        device = selected_repository.authenticate(
            token,
            request.remote_addr or "unknown",
            credential_version,
            allow_repair=allow_repair,
        )
    except DeviceRateLimitError as exc:
        raise TooManyRequests(
            description="DEVICE-007 裝置驗證嘗試過多，請稍後再試",
            retry_after=exc.retry_after_seconds,
        ) from None
    if device is None:
        raise Unauthorized(description="DEVICE-001 裝置驗證失敗")
    g.device = device
    return device
