"""Server-owned content policy; a model classification is never a decision."""

from __future__ import annotations

from inktime.app.domain.analysis.schema import DISPLAY_EXCLUSION_CATEGORIES

CONTENT_FILTER_DEFAULTS = {
    "analysis.exclude_sexualized_content": True,
    "analysis.exclude_explicit_nudity": True,
    "analysis.exclude_female_glamour_portraits": True,
    "analysis.content_filter_min_confidence": 0.85,
    "analysis.female_glamour_min_confidence": 0.90,
}
CONTENT_FILTER_RULE_VERSION = "content-filter-v4-multilabel"
STRICT_CONTENT_FILTER_RULE_VERSION = "content-filter-v5-strict-display"
DISPLAY_EXCLUSION_RULE_VERSION = "display-exclusion-v1"
DISPLAY_RISK_MIN_CONFIDENCE = 0.60
CONTENT_FILTER_SWITCHES = {
    "sexualized_content": "analysis.exclude_sexualized_content",
    "explicit_nudity": "analysis.exclude_explicit_nudity",
    "female_glamour_portrait": "analysis.exclude_female_glamour_portraits",
}


def evaluate_content_filter(content_filter: dict, settings=None, *, strict: bool = False) -> dict:
    policy = CONTENT_FILTER_DEFAULTS | dict(settings or {})
    classifications = {}
    matched_codes = []
    uncertain_codes = []
    for code, switch in CONTENT_FILTER_SWITCHES.items():
        classification = content_filter[code]
        threshold_key = (
            "analysis.female_glamour_min_confidence"
            if code == "female_glamour_portrait"
            else "analysis.content_filter_min_confidence"
        )
        confidence = float(classification["confidence"])
        # A v6 display safety decision cannot be weakened by old preference
        # switches. Earlier analyses retain their original policy semantics.
        threshold = DISPLAY_RISK_MIN_CONFIDENCE if strict else float(policy[threshold_key])
        enabled = True if strict else bool(policy[switch])
        matched = bool(classification["detected"]) and enabled and confidence >= threshold
        uncertain = bool(classification["detected"]) and strict and confidence < threshold
        value = {
            "detected": classification["detected"], "confidence": confidence,
            "threshold": threshold, "enabled": enabled, "matched": matched,
        }
        if strict:
            value["uncertain"] = uncertain
        classifications[code] = value
        if matched:
            matched_codes.append(code)
        elif uncertain:
            uncertain_codes.append(code)
    result = {
        "decision": "auto_excluded" if matched_codes else "pending_review" if uncertain_codes else "pass",
        "primary_reason": matched_codes[0] if matched_codes else uncertain_codes[0] if uncertain_codes else "passed",
        "matched_codes": matched_codes,
        "classifications": classifications,
        "rule": "content-filter",
        "rule_version": STRICT_CONTENT_FILTER_RULE_VERSION if strict else CONTENT_FILTER_RULE_VERSION,
    }
    if strict:
        result["uncertain_codes"] = uncertain_codes
    return result


def evaluate_display_exclusion(display_exclusion: dict) -> dict:
    """Turn the compact v6 visual category into a fail-closed display decision."""
    category = str(display_exclusion["category"])
    confidence = float(display_exclusion["confidence"])
    if category not in DISPLAY_EXCLUSION_CATEGORIES or not 0 <= confidence <= 1:
        raise ValueError("display_exclusion 必須符合 v6 Schema")
    if category == "none":
        decision = "pass" if confidence >= DISPLAY_RISK_MIN_CONFIDENCE else "pending_review"
    elif confidence >= DISPLAY_RISK_MIN_CONFIDENCE:
        decision = "auto_excluded"
    else:
        decision = "pending_review"
    return {
        "decision": decision,
        "primary_reason": category if category != "none" else "passed" if decision == "pass" else "display_risk_uncertain",
        "category": category,
        "confidence": confidence,
        "threshold": DISPLAY_RISK_MIN_CONFIDENCE,
        "rule": "display-exclusion",
        "rule_version": DISPLAY_EXCLUSION_RULE_VERSION,
    }
