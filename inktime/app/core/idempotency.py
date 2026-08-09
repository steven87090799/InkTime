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


def request_fingerprint(value: Any) -> str:
    """Hash the canonical JSON request material stored with a job."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
