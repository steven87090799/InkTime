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



def release_source_content_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    """Use frozen source evidence; never infer old content from a live photo."""
    options = manifest.get("render_options")
    plans = options.get("render_plans") if isinstance(options, dict) else None
    result: dict[str, str] = {}
    conflicts = set()
    for plan in plans if isinstance(plans, list) else []:
        if not isinstance(plan, dict):
            continue
        for role in ("primary", "secondary"):
            photo_id, digest = plan.get(role + "_photo_id"), plan.get(role + "_sha256")
            if not isinstance(photo_id, str) or not isinstance(digest, str):
                continue
            digest = digest.casefold()
            if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
                continue
            if photo_id in result and result[photo_id] != digest:
                conflicts.add(photo_id)
            result[photo_id] = digest
    return {key: value for key, value in result.items() if key not in conflicts}
