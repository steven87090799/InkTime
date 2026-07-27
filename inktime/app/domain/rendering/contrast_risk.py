"""Conservative Spectra 6 contrast risk classification.

Thresholds are intentionally deterministic and must be calibrated against a
physical Spectra 6 panel before being treated as colour-accuracy guarantees.
"""
from __future__ import annotations

from typing import Any, Mapping


EPAPER_CONTRAST_RISK_VERSION = "epaper-contrast-risk-v1"


def calculate_epaper_contrast_risk(photo: Mapping[str, Any]) -> str:
    """Classify only from persisted local/E6 features; never change ranking."""
    values = {key: photo[key] for key in (
        "brightness", "contrast", "underexposed_ratio", "e6_score",
        "e6_contrast_score", "e6_subject_score",
    )}
    if any(value is None for value in values.values()):
        return "low"
    brightness = float(values["brightness"])
    contrast = float(values["contrast"])
    under = float(values["underexposed_ratio"])
    e6 = float(values["e6_score"])
    e6_contrast = float(values["e6_contrast_score"])
    e6_subject = float(values["e6_subject_score"])
    if under >= .65 or contrast < 10 or e6 < 28 or e6_contrast < 25 or e6_subject < 22:
        return "high"
    if under >= .40 or brightness < 55 or contrast < 18 or e6 < 45 or e6_contrast < 42:
        return "medium"
    return "low"
