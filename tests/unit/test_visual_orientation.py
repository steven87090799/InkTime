import pytest
from PIL import Image

from inktime.app.domain.analysis.schema import AnalysisValidationError, validate_analysis_result
from inktime.app.domain.photos.orientation import resolve_effective_orientation


def test_orientation_priority_and_thresholds():
    assert resolve_effective_orientation(exif_orientation=6, manual_rotation_cw=None, ai_rotation_cw=90, ai_confidence=.95).source == "ai"
    assert resolve_effective_orientation(exif_orientation=6, manual_rotation_cw=None, ai_rotation_cw=180, ai_confidence=.95).source == "exif_normalized"
    assert resolve_effective_orientation(exif_orientation=1, manual_rotation_cw=270, ai_rotation_cw=90, ai_confidence=1).as_dict()["source"] == "manual"
    assert resolve_effective_orientation(exif_orientation=8, manual_rotation_cw=None, ai_rotation_cw=90, ai_confidence=.99, ai_ambiguous=True).rotation_degrees == 0


def test_orientation_validation_rejects_invalid_values():
    result = {"schema_version": 1, "caption": "x", "types": ["其他"], "memory_score": 1, "beauty_score": 1, "technical_quality_score": 1, "emotion_score": 1, "side_caption": "", "should_keep": True, "sensitive": False, "reason": "x", "visual_orientation": {"rotation_cw": 45, "confidence": 1, "ambiguous": False, "evidence": ["faces_upright"]}}
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(result)


def test_pillow_clockwise_and_counterclockwise_rotation_pixels():
    image = Image.new("RGB", (2, 3), "white")
    image.putpixel((0, 0), (255, 0, 0))
    assert image.rotate(-90, expand=True).getpixel((2, 0)) == (255, 0, 0)
    assert image.rotate(-270, expand=True).getpixel((0, 1)) == (255, 0, 0)
