"""Deterministic, versioned local quality decisions.

This module deliberately consumes only local metadata and measured features.  It
is shared by the scanner and the analysis prefilter so a photo cannot receive
two contradictory automatic decisions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping


FEATURE_VERSION = "local-quality-v5"
_SCREENSHOT_WORDS = {
    "screenshot",
    "screen shot",
    "screen capture",
    "螢幕快照",
    "螢幕擷取",
    "截圖",
    "截屏",
    "スクリーンショット",
    "화면 캡처",
}
_DOCUMENT_TOKENS = {"receipt", "invoice", "document", "scan", "收據", "發票", "文件"}
_SOCIAL_SOFTWARE = ("instagram", "line", "wechat", "facebook")
_TOKEN_RE = re.compile(r"[_\W]+", re.UNICODE)


def _value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _exif_software(row: Mapping[str, Any]) -> str:
    raw = _value(row, "exif_json", "")
    if not raw:
        return ""
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    return " ".join(
        str(value) for key, value in parsed.items() if "software" in str(key).casefold()
    ).casefold()


def _filename_tokens(relative_path: str) -> set[str]:
    stem = Path(relative_path).stem.casefold()
    return {token for token in _TOKEN_RE.split(stem) if token}


def is_confirmed_screenshot(row: Mapping[str, Any]) -> bool:
    """Return true only for explicit, high-confidence screenshot evidence.

    This predicate is also used at the final AI send boundary.  It deliberately
    excludes heuristic-only screen dimensions so an ordinary camera photo is
    never blocked solely because it happens to share a phone aspect ratio.
    """
    relative_path = str(_value(row, "relative_path", ""))
    filename = Path(relative_path).name.casefold()
    software = _exif_software(row)
    score = float(_value(row, "screenshot_likelihood", 0.0) or 0.0)
    return (
        any(word in filename for word in _SCREENSHOT_WORDS)
        or any(word in software for word in _SCREENSHOT_WORDS)
        or score >= 0.95
    )


def local_candidate_score(row: Mapping[str, Any], *, evaluation: Mapping[str, Any] | None = None) -> float:
    """One shared technical ranking score for scanner and analysis selection."""
    blur = max(0.0, float(_value(row, "blur_score", 0.0) or 0.0))
    contrast = max(0.0, min(100.0, float(_value(row, "contrast", 0.0) or 0.0)))
    exposure = max(
        float(_value(row, "overexposed_ratio", 0.0) or 0.0),
        float(_value(row, "underexposed_ratio", 0.0) or 0.0),
    )
    short_edge = min(int(_value(row, "width", 0) or 0), int(_value(row, "height", 0) or 0))
    score = blur**0.5 * 3.2 + min(32.0, contrast * 0.8) + min(12.0, short_edge / 100.0) - exposure * 45
    decision = (evaluation or {}).get("decision")
    if decision == "auto_excluded":
        return 0.0
    if decision == "low_priority":
        score -= 15.0
    return round(max(0.0, min(100.0, score)), 2)


def evaluate_local_quality(
    row: Mapping[str, Any], *, settings: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Evaluate every check, then choose one stable primary reason.

    ``auto_excluded`` is intentionally reserved for high-confidence local
    evidence.  Ambiguous screenshots, social exports, and merely poor photos
    remain candidates at reduced priority.
    """
    relative_path = str(_value(row, "relative_path", ""))
    width, height = int(_value(row, "width", 0) or 0), int(_value(row, "height", 0) or 0)
    short_edge = min(width, height) if width and height else 0
    fmt = str(_value(row, "format", "")).casefold()
    camera = bool(str(_value(row, "camera_make", "")).strip() or str(_value(row, "camera_model", "")).strip())
    software = _exif_software(row)
    screenshot_score = float(_value(row, "screenshot_likelihood", 0.0) or 0.0)
    blur_raw = _value(row, "blur_score")
    contrast_raw = _value(row, "contrast")
    over_raw = _value(row, "overexposed_ratio")
    under_raw = _value(row, "underexposed_ratio")
    blur = float(blur_raw or 0.0)
    contrast = float(contrast_raw or 0.0)
    over = float(over_raw or 0.0)
    under = float(under_raw or 0.0)
    file_size_raw = _value(row, "file_size")
    file_size = int(file_size_raw or 0)
    strong_screen = is_confirmed_screenshot(row)
    screen_dimensions = (width, height) in {(1080, 1920), (1920, 1080), (1170, 2532), (2532, 1170)}
    screen_ratio = bool(width and height and abs(max(width, height) / min(width, height) - 16 / 9) < 0.035)
    independent = [
        ("screen_dimensions", 0.50, screen_dimensions),
        ("screen_ratio", 0.15, screen_ratio),
        ("no_camera", 0.20, not camera),
        ("png", 0.10, fmt == "png"),
    ]
    # Dimensions and ratio are one signal family, never two independent votes.
    family_count = int(screen_dimensions or screen_ratio) + sum(
        int(hit) for key, _weight, hit in independent if key not in {"screen_dimensions", "screen_ratio"}
    )
    score = max(0.50 if screen_dimensions else 0.0, 0.15 if screen_ratio else 0.0)
    score += sum(
        weight for key, weight, hit in independent if key not in {"screen_dimensions", "screen_ratio"} and hit
    )
    ordered_tokens = [token for token in _TOKEN_RE.split(Path(relative_path).stem.casefold()) if token]
    # A trailing word in an ordinary filename (landscape_scan,
    # my_document_of_the_trip) is too ambiguous.  Accept an explicit leading
    # token/prefix, or a filename made only of document terms.
    document_prefix = ordered_tokens[0] if ordered_tokens and ordered_tokens[0] in _DOCUMENT_TOKENS else None
    document_token = bool(document_prefix) or bool(ordered_tokens and set(ordered_tokens) <= _DOCUMENT_TOKENS)
    scanner_evidence = "scanner" in software or "camscanner" in software
    social = any(name in software for name in _SOCIAL_SOFTWARE) or (
        not camera and fmt in {"jpeg", "jpg"} and short_edge in {1024, 1280}
    )
    exposure = max(over, under)
    config = settings or {}
    enabled = bool(config.get("analysis.prefilter_enabled", True))
    screenshots_enabled = bool(config.get("analysis.prefilter_screenshots", True))
    low_quality_enabled = bool(config.get("analysis.prefilter_low_quality", True))
    e6_enabled = bool(config.get("analysis.e6_prefilter_enabled", True))
    e6_threshold = float(config.get("analysis.e6_min_score", 25))
    sensitivity = str(config.get("analysis.prefilter_sensitivity", "conservative"))
    protected = bool(_value(row, "favorite", False) or _value(row, "manual_override", False)) or str(
        _value(row, "exclusion_status", "")
    ) in {"manually_restored", "manually_excluded"}
    e6_score = _value(row, "e6_score")
    screenshot_threshold = {"conservative": 0.90, "balanced": 0.75, "aggressive": 0.60}.get(sensitivity, 0.90)
    checks = {
        "screenshot_strong": strong_screen,
        "screenshot_mixed_signal": (
            not camera and family_count >= 3 and screenshot_score >= screenshot_threshold
        ),
        "document_token_with_evidence": bool(document_token and (scanner_evidence or not camera)),
        # Laplacian variance alone can be low for intentionally smooth photos,
        # so blur is only decisive together with very low global contrast.
        # These conservative v5 thresholds catch the observed 44.54 / 11.80
        # out-of-focus frame while leaving the next-lowest real photo in the
        # current library (324.05 / 26.70) well outside the exclusion boundary.
        "severe_blur": blur_raw is not None and contrast_raw is not None and blur < 60 and contrast < 15,
        "suspected_blur": blur_raw is not None and contrast_raw is not None and blur < 120 and contrast < 18,
        "short_edge_under_240": bool(short_edge and short_edge < 240),
        "tiny_empty": file_size_raw is not None
        and contrast_raw is not None
        and file_size < 4096
        and contrast < 8,
        "small_compressed": file_size_raw is not None and file_size < 10 * 1024 and fmt not in {"png", "bmp"},
        "extreme_exposure_low_contrast": over_raw is not None
        and under_raw is not None
        and contrast_raw is not None
        and exposure >= 0.92
        and contrast < 8,
        "exposure_low_priority": (over_raw is not None or under_raw is not None) and exposure >= 0.60,
        "social_export": social,
        "e6_low": e6_score is not None and float(e6_score) < e6_threshold,
    }
    matched = [key for key, hit in checks.items() if hit]
    excluded_reasons: list[str] = []
    if low_quality_enabled and checks["short_edge_under_240"]:
        excluded_reasons.append("resolution_too_low")
    if checks["screenshot_strong"] or (screenshots_enabled and checks["screenshot_mixed_signal"]):
        excluded_reasons.append("screenshot")
    if low_quality_enabled and checks["document_token_with_evidence"]:
        excluded_reasons.append("document_or_receipt")
    if low_quality_enabled and checks["severe_blur"]:
        excluded_reasons.append("severe_blur")
    if low_quality_enabled and checks["tiny_empty"]:
        excluded_reasons.append("tiny_nearly_blank")
    if low_quality_enabled and checks["extreme_exposure_low_contrast"]:
        excluded_reasons.append("extreme_exposure_low_contrast")
    if e6_enabled and checks["e6_low"]:
        excluded_reasons.append("e6_below_threshold")
    low_reasons = [
        key
        for key in ("suspected_blur", "small_compressed", "exposure_low_priority", "social_export")
        if low_quality_enabled and checks[key]
    ]
    decision = (
        "auto_excluded"
        if checks["screenshot_strong"]
        else "disabled"
        if not enabled
        else "protected"
        if protected
        else "auto_excluded"
        if excluded_reasons
        else "low_priority"
        if low_reasons
        else "pass"
    )
    primary = (excluded_reasons or low_reasons or ["passed"])[0]
    return {
        "decision": decision,
        "primary_reason": primary,
        "matched_checks": matched,
        "thresholds": {
            "screenshot_score": screenshot_threshold,
            "screenshot_signals": 3,
            "short_edge": 240,
            "severe_blur": [60, 15],
            "suspected_blur": [120, 18],
            "exposure_low_priority": 0.60,
            "e6_min_score": e6_threshold,
        },
        "sensitivity": sensitivity,
        "feature_version": FEATURE_VERSION,
        "e6_feature_version": "e6-prefilter-v1",
        "e6_threshold": e6_threshold,
        "evidence": {
            "width": width,
            "height": height,
            "short_edge": short_edge,
            "format": fmt,
            "camera_metadata": camera,
            "software": software[:256],
            "screenshot_likelihood": screenshot_score,
            "screenshot_signal_score": round(score, 2),
            "screenshot_independent_signals": family_count,
            "blur": blur,
            "contrast": contrast,
            "overexposed_ratio": over,
            "underexposed_ratio": under,
            "file_size": file_size,
            "checks": checks,
        },
    }
