"""Server-owned content policy; a model classification is never a decision."""

from __future__ import annotations

CONTENT_FILTER_DEFAULTS = {
    "analysis.exclude_sexualized_content": True,
    "analysis.exclude_explicit_nudity": True,
    "analysis.exclude_female_glamour_portraits": True,
    "analysis.content_filter_min_confidence": 0.85,
    "analysis.female_glamour_min_confidence": 0.90,
}
CONTENT_FILTER_RULE_VERSION = "content-filter-v4-multilabel"
CONTENT_FILTER_SWITCHES = {
    "sexualized_content": "analysis.exclude_sexualized_content",
    "explicit_nudity": "analysis.exclude_explicit_nudity",
    "female_glamour_portrait": "analysis.exclude_female_glamour_portraits",
}


def evaluate_content_filter(content_filter: dict, settings=None) -> dict:
    policy = CONTENT_FILTER_DEFAULTS | dict(settings or {})
    classifications = {}
    matched_codes = []
    for code, switch in CONTENT_FILTER_SWITCHES.items():
        classification = content_filter[code]
        threshold_key = (
            "analysis.female_glamour_min_confidence"
            if code == "female_glamour_portrait"
            else "analysis.content_filter_min_confidence"
        )
        confidence = float(classification["confidence"])
        threshold = float(policy[threshold_key])
        enabled = bool(policy[switch])
        matched = bool(classification["detected"]) and enabled and confidence >= threshold
        classifications[code] = {
            "detected": classification["detected"], "confidence": confidence,
            "threshold": threshold, "enabled": enabled, "matched": matched,
        }
        if matched:
            matched_codes.append(code)
    return {
        "decision": "auto_excluded" if matched_codes else "pass",
        "primary_reason": matched_codes[0] if matched_codes else "passed",
        "matched_codes": matched_codes,
        "classifications": classifications,
        "rule": "content-filter",
        "rule_version": CONTENT_FILTER_RULE_VERSION,
    }
