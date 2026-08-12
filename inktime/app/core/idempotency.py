from __future__ import annotations

from hashlib import sha256
import json
from typing import Any


def scoped_idempotency_key(endpoint: str, actor: str, raw_key: str | None) -> str | None:
    """Return an actor-scoped durable identity for an expensive POST."""

    key = str(raw_key or "").strip()[:128]
    if not key:
        return None
    actor_digest = sha256(str(actor).encode("utf-8")).hexdigest()[:24]
    return f"idempotency:{endpoint}:{actor_digest}:{key}"


def grouped_idempotency_key(raw_key: str | None, group: str) -> str | None:
    """Derive a bounded, deterministic identity for a grouped AI request.

    Keep the existing short ``client-key:group`` representation when the
    delimiter is unambiguous and the shared helper cannot normalize away any
    part of the group.  Otherwise hash the normalized client key and exact
    group together so a later 128-character bound cannot erase the group.
    """

    key = str(raw_key or "").strip()[:128]
    if not key:
        return None
    group_value = str(group)
    legacy = f"{key}:{group_value}"
    if (
        ":" not in key
        and ":" not in group_value
        and group_value == group_value.strip()
        and len(legacy) <= 128
    ):
        return legacy

    canonical = json.dumps(
        {
            "domain": "ai-mode-run/full-library/group",
            "group": group_value,
            "key": key,
            "version": 1,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    key_hint = "".join(character if character.isalnum() else "_" for character in key[:24]) or "key"
    return f"{key_hint}:g1:{digest}"


def request_fingerprint(value: Any) -> str:
    """Hash the canonical JSON request material stored with a job."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
