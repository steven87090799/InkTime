"""Read both source photos of a composed frame from a release manifest."""

from __future__ import annotations

from typing import Any


def release_source_photo_ids(manifest: dict[str, Any]) -> list[str]:
    options = manifest.get("render_options") or {}
    plans = options.get("render_plans") or []
    identifiers = [
        str(plan[key]) for plan in plans for key in ("primary_photo_id", "secondary_photo_id")
        if isinstance(plan, dict) and plan.get(key)
    ]
    if not identifiers:
        identifiers = [
            identifier for item in manifest.get("files", [])
            for identifier in str(item.get("source_photo_id") or "").split("+") if identifier
        ]
    return list(dict.fromkeys(identifiers))
