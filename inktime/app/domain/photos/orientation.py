"""One canonical interpretation of EXIF, model advice, and a human override."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

ROTATIONS = {0, 90, 180, 270}
EVIDENCE = {"faces_upright", "text_upright", "horizon_level", "gravity_objects", "architecture_vertical", "insufficient_visual_cues"}


@dataclass(frozen=True)
class EffectiveOrientation:
    rotation_degrees: int
    source: str
    confidence: float | None
    exif_normalized: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_effective_orientation(*, exif_orientation: int | None, manual_rotation_cw: int | None,
                                  ai_rotation_cw: int | None, ai_confidence: float | None,
                                  ai_ambiguous: bool = False) -> EffectiveOrientation:
    """EXIF is applied exactly once by Pillow; this returns only an extra CW rotation."""
    exif_normalized = exif_orientation in {2, 3, 4, 5, 6, 7, 8}
    if manual_rotation_cw in ROTATIONS:
        return EffectiveOrientation(manual_rotation_cw, "manual", None, exif_normalized)
    confidence = float(ai_confidence) if ai_confidence is not None else None
    threshold = 0.98 if ai_rotation_cw == 180 else 0.95
    if ai_rotation_cw in ROTATIONS and not ai_ambiguous and confidence is not None and confidence >= threshold:
        return EffectiveOrientation(ai_rotation_cw, "ai", confidence, exif_normalized)
    return EffectiveOrientation(0, "exif_normalized" if exif_normalized else "none", confidence, exif_normalized)
