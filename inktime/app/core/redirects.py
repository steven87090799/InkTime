"""Redirect target validation for browser-facing error flows."""

from __future__ import annotations

from urllib.parse import urlparse


def safe_local_redirect_target(value: str | None, *, allowed_host: str | None = None) -> str | None:
    """Accept only a local path or same-origin absolute URL."""

    candidate = str(value or "").strip()
    if not candidate or any(ord(char) < 0x20 for char in candidate):
        return None
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.username or parsed.password or not allowed_host:
            return None
        if parsed.netloc.casefold() != allowed_host.casefold():
            return None
        return candidate
    if not candidate.startswith("/") or candidate.startswith("//"):
        return None
    return candidate
