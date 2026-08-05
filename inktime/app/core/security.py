from __future__ import annotations

import hashlib
from hashlib import sha256
import hmac
import json
import re
import secrets
import threading
from collections import OrderedDict
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from inktime.app.domain.auth import validate_password


SENSITIVE_KEY = re.compile(
    r"^(?:api[_-]?key|token|password|secret|authorization|cookie|session|bearer|(?:previous[_-]?)?device[_-]?(?:credential|token|secret)(?:[_-]?(?:hash|ciphertext))?|pairing[_-]?(?:code|nonce)(?:[_-]?(?:hash|ciphertext))?)$",
    re.IGNORECASE,
)
SENSITIVE_TEXT = re.compile(
    r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}|\b(?:sk-|itd_|ids_)[A-Za-z0-9._~-]{8,}|\b(?:api[_-]?key|token|authorization|pairing[_-]?(?:code|nonce))=([^\s&]+)"
)
PRIVATE_PATH = re.compile(r"(?:/Users/[^\s]+|/home/[^\s]+|/photos/[^\s]+)")
GPS = re.compile(r"(?<!\d)(?:-?\d{1,2}\.\d{4,})\s*[,，]\s*(?:-?\d{1,3}\.\d{4,})(?!\d)")
BASE64 = re.compile(r"(?:data:image/[^;]+;base64,|[A-Za-z0-9+/]{256,}={0,2})")
_REGISTERED_SECRETS: OrderedDict[str, None] = OrderedDict()
_SECRET_LOCK = threading.RLock()


def register_secret(value: str) -> None:
    """讓 formatter 能遮蔽純文字 exception 中已知的完整 credential。"""

    if len(value) < 4:
        return
    with _SECRET_LOCK:
        _REGISTERED_SECRETS.pop(value, None)
        _REGISTERED_SECRETS[value] = None
        while len(_REGISTERED_SECRETS) > 256:
            _REGISTERED_SECRETS.popitem(last=False)


def redact_text(value: str) -> str:
    result = str(value)
    with _SECRET_LOCK:
        registered = sorted(_REGISTERED_SECRETS, key=len, reverse=True)
    for secret in registered:
        result = result.replace(secret, "[已遮蔽]")
    result = SENSITIVE_TEXT.sub(
        lambda match: f"{match.group(1)}[已遮蔽]" if match.group(1) else "[已遮蔽]",
        result,
    )
    return BASE64.sub("[已遮蔽圖片資料]", GPS.sub("[已遮蔽 GPS]", PRIVATE_PATH.sub("[已遮蔽路徑]", result)))


def hash_password(password: str) -> str:
    password = validate_password(password)
    method = "scrypt" if hasattr(hashlib, "scrypt") else "pbkdf2:sha256:600000"
    return generate_password_hash(password, method=method)


def verify_password(password_hash: str, password: str) -> bool:
    return check_password_hash(password_hash, password)


def issue_device_token() -> str:
    token = "itd_" + secrets.token_urlsafe(32)
    register_secret(token)
    return token


def issue_device_secret() -> str:
    """Issue the long-lived credential returned by a one-time device claim."""

    secret = "ids_" + secrets.token_urlsafe(48)
    register_secret(secret)
    return secret


def hash_device_token(token: str, pepper: str) -> str:
    """Hash the deployed Legacy/Stock token contract without changing it."""

    return hmac.new(pepper.encode("utf-8"), token.encode("utf-8"), sha256).hexdigest()


def _hash_domain(value: str, pepper: str, purpose: str) -> str:
    material = f"{purpose}\x00{value}".encode("utf-8")
    return hmac.new(pepper.encode("utf-8"), material, sha256).hexdigest()


def hash_device_secret(secret: str, pepper: str) -> str:
    """Hash a high-entropy Device Secret without storing it in the database."""

    return _hash_domain(secret, pepper, "device-secret-v1")


def hash_pairing_code(code: str, pepper: str) -> str:
    return _hash_domain(code, pepper, "pairing-code-v1")


def hash_pairing_nonce(nonce: str, pepper: str) -> str:
    return _hash_domain(nonce, pepper, "pairing-nonce-v1")


def hash_pairing_scope(scope: str, value: str, pepper: str) -> str:
    return _hash_domain(value, pepper, f"pairing-scope-{scope}-v1")


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
    if isinstance(value, str):
        return redact_text(value)
    return value


def safe_json(value: Any) -> str:
    return json.dumps(redact(value), ensure_ascii=False, default=str)
