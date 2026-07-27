from __future__ import annotations

from inktime.app.domain.photos.quality_policy import evaluate_local_quality, local_candidate_score


def test_quality_policy_requires_combined_screenshot_evidence_and_protects_manual_photo():
    base = {"relative_path": "camera/photo.png", "width": 1080, "height": 1920, "format": "png", "camera_make": "Nikon"}
    assert evaluate_local_quality(base)["decision"] == "pass"
    assert evaluate_local_quality(
        {"relative_path": "plain.png", "width": 1080, "height": 1920, "format": "png"}
    )["decision"] == "pass"
    screenshot = evaluate_local_quality(
        {**base, "relative_path": "Screenshot_2026.png", "camera_make": "", "camera_model": ""}
    )
    assert screenshot["decision"] == "auto_excluded"
    assert screenshot["primary_reason"] == "screenshot"
    assert evaluate_local_quality({**base, "favorite": True})["decision"] == "protected"


def test_quality_policy_document_e6_and_low_priority_score_are_deterministic():
    document = evaluate_local_quality(
        {"relative_path": "receipt_2026.jpg", "width": 1200, "height": 1800, "format": "jpg"}
    )
    assert document["decision"] == "auto_excluded"
    e6 = evaluate_local_quality({"relative_path": "photo.jpg", "width": 1200, "height": 800, "e6_score": 10})
    assert e6["decision"] == "auto_excluded"
    score = local_candidate_score({"width": 1200, "height": 800, "blur_score": 36, "contrast": 30})
    assert local_candidate_score({"width": 1200, "height": 800, "blur_score": 36, "contrast": 30}, evaluation=e6) == score
