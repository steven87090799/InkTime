from __future__ import annotations

from flask import current_app, request
from werkzeug.exceptions import TooManyRequests, Unauthorized

from inktime.app.repositories.devices import DeviceRateLimitError, DeviceRepository


def authenticate_device_request(*, repository: DeviceRepository | None = None):
    """Authenticate every Device Bearer endpoint through one stable contract."""

    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise Unauthorized(description="DEVICE-001 裝置驗證失敗")
    token = authorization[7:].strip()
    if not token:
        raise Unauthorized(description="DEVICE-001 裝置驗證失敗")
    selected_repository = repository or current_app.extensions["inktime_device_repository"]
    try:
        device = selected_repository.authenticate(
            token,
            request.remote_addr or "unknown",
        )
    except DeviceRateLimitError as exc:
        raise TooManyRequests(
            description="DEVICE-007 裝置驗證嘗試過多，請稍後再試",
            retry_after=exc.retry_after_seconds,
        ) from None
    if device is None:
        raise Unauthorized(description="DEVICE-001 裝置驗證失敗")
    return device
