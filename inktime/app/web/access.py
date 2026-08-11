from __future__ import annotations

from functools import wraps
import logging
import secrets

from flask import abort, g, request, session

from inktime.app.core.logging import log_event, should_log_rate_limited


LOGGER = logging.getLogger("security")


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def verify_csrf() -> None:
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(str(supplied), str(expected)):
        if should_log_rate_limited("csrf-rejected", interval_seconds=10):
            log_event(
                LOGGER,
                logging.WARNING,
                "CSRF validation rejected",
                event="csrf_rejected",
                error_code="AUTH-002",
                retryable=False,
            )
        abort(403, description="AUTH-002 CSRF 驗證失敗")


def login_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if getattr(g, "user", None) is None:
            log_event(
                LOGGER,
                logging.DEBUG,
                "Authentication is required",
                event="authorization_denied",
                error_code="AUTH-003",
                details={"reason": "anonymous"},
            )
            abort(401, description="AUTH-003 請先登入")
        return function(*args, **kwargs)

    return wrapped


def administrator_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        user = getattr(g, "user", None)
        if user is None:
            log_event(
                LOGGER,
                logging.DEBUG,
                "Administrator authorization denied",
                event="authorization_denied",
                error_code="AUTH-003",
                details={"reason": "anonymous"},
            )
            abort(401, description="AUTH-003 請先登入")
        if user["role"] != "administrator":
            if should_log_rate_limited("authorization-denied-role", interval_seconds=10):
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "Administrator authorization denied",
                    event="authorization_denied",
                    error_code="AUTH-004",
                    details={"actor_type": str(user["role"]), "reason": "insufficient_role"},
                )
            abort(403, description="AUTH-004 權限不足")
        return function(*args, **kwargs)

    return wrapped
