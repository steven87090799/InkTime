from __future__ import annotations

import hashlib
from hashlib import sha256
import hmac
import json
import re
import secrets
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash


SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|token|password|secret|authorization|cookie|session)", re.IGNORECASE
)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("密碼至少需要 12 個字元")
    method = "scrypt" if hasattr(hashlib, "scrypt") else "pbkdf2:sha256:600000"
    return generate_password_hash(password, method=method)


def verify_password(password_hash: str, password: str) -> bool:
    return check_password_hash(password_hash, password)


def issue_device_token() -> str:
    return "itd_" + secrets.token_urlsafe(32)


def hash_device_token(token: str, pepper: str) -> str:
    return hmac.new(pepper.encode("utf-8"), token.encode("utf-8"), sha256).hexdigest()


def mask_secret(value: str) -> str:
    if not value:
        return "未設定"
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}…{value[-4:]}"


def redact(value: Any) -> Any:
    """遞迴遮蔽診斷與 Log 中常見敏感欄位。"""
    if isinstance(value, dict):
        return {
            str(key): "[已遮蔽]" if SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def safe_json(value: Any) -> str:
    return json.dumps(redact(value), ensure_ascii=False, default=str)
