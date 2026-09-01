"""Server-owned content policy; a model classification is never a decision."""

from __future__ import annotations

CONTENT_FILTER_DEFAULTS = {
    "analysis.exclude_sexualized_content": True,
    "analysis.exclude_explicit_nudity": True,
    "analysis.exclude_female_glamour_portraits": True,
    "analysis.content_filter_min_confidence": 0.85,
    "analysis.female_glamour_min_confidence": 0.90,
}
CONTENT_FILTER_RULE_VERSION = "content-filter-v4"
CONTENT_FILTER_SWITCHES = {
    "sexualized_content": "analysis.exclude_sexualized_content",
    "explicit_nudity": "analysis.exclude_explicit_nudity",
    "female_glamour_portrait": "analysis.exclude_female_glamour_portraits",
}


def evaluate_content_filter(content_filter: dict, settings=None) -> dict:
    policy = CONTENT_FILTER_DEFAULTS | dict(settings or {})
    code = content_filter["exclude_code"]
    confidence = float(content_filter["confidence"])
    threshold_key = (
        "analysis.female_glamour_min_confidence"
        if code == "female_glamour_portrait"
        else "analysis.content_filter_min_confidence"
    )
    threshold = float(policy[threshold_key])
    enabled = bool(policy.get(CONTENT_FILTER_SWITCHES.get(code, ""), False))
    return {
        "decision": "auto_excluded" if enabled and confidence >= threshold else "pass",
        "primary_reason": code,
        "confidence": confidence,
        "threshold": threshold,
        "enabled": enabled,
        "rule": "content-filter",
        "rule_version": CONTENT_FILTER_RULE_VERSION,
    }
