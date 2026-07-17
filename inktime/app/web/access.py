from __future__ import annotations

from functools import wraps
import secrets

from flask import abort, g, request, session


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
        abort(403, description="AUTH-002 CSRF 驗證失敗")


def login_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if getattr(g, "user", None) is None:
            abort(401, description="AUTH-003 請先登入")
        return function(*args, **kwargs)

    return wrapped


def administrator_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        user = getattr(g, "user", None)
        if user is None:
            abort(401, description="AUTH-003 請先登入")
        if user["role"] != "administrator":
            abort(403, description="AUTH-004 權限不足")
        return function(*args, **kwargs)

    return wrapped
