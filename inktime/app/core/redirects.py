"""Redirect target validation for browser-facing error flows."""

from __future__ import annotations

from urllib.parse import urlparse


def safe_local_redirect_target(
    value: str | None,
    *,
    allowed_host: str | None = None,
    allowed_scheme: str | None = None,
) -> str | None:
    """Accept only a local path or same-origin absolute URL."""

    candidate = str(value or "").strip()
    if (
        not candidate
        or any(ord(char) < 0x20 or ord(char) == 0x7f for char in candidate)
        or "\\" in candidate
    ):
        return None
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        try:
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
            return None
        if parsed.username or parsed.password or not allowed_host:
            return None
        if allowed_scheme and parsed.scheme.casefold() != str(allowed_scheme).casefold():
            return None
        expected = urlparse(f"//{allowed_host}")
        try:
            expected_port = expected.port
        except ValueError:
            return None
        if expected.hostname is None or hostname.casefold() != expected.hostname.casefold():
            return None
        if port != expected_port:
            return None
        return candidate
    if not candidate.startswith("/") or candidate.startswith("//"):
        return None
    return candidate
