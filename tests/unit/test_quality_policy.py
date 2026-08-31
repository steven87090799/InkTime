from __future__ import annotations

from inktime.app.domain.photos.quality_policy import evaluate_local_quality, local_candidate_score


def test_quality_policy_requires_combined_screenshot_evidence_and_protects_manual_photo():
    base = {
        "relative_path": "camera/photo.png",
        "width": 1080,
        "height": 1920,
        "format": "png",
        "camera_make": "Nikon",
    }
    assert evaluate_local_quality(base)["decision"] == "pass"
    assert (
        evaluate_local_quality(
            {"relative_path": "plain.png", "width": 1080, "height": 1920, "format": "png"}
        )["decision"]
        == "pass"
    )
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
    assert (
        local_candidate_score(
            {"width": 1200, "height": 800, "blur_score": 36, "contrast": 30},
            evaluation=e6,
        )
        == 0
    )


def test_quality_policy_v5_preserves_explicit_screenshot_evidence_and_rejects_observed_blur():
    renamed_screenshot = {
        "relative_path": "renamed-export.png",
        "width": 1034,
        "height": 802,
        "format": "png",
        "screenshot_likelihood": 1.0,
        "blur_score": 1638.33,
        "contrast": 27.51,
    }
    screenshot = evaluate_local_quality(renamed_screenshot)
    assert screenshot["decision"] == "auto_excluded"
    assert screenshot["primary_reason"] == "screenshot"
    assert evaluate_local_quality({**renamed_screenshot, "favorite": True})["decision"] == "auto_excluded"
    assert local_candidate_score(renamed_screenshot, evaluation=screenshot) == 0

    observed_blurry_frame = {
        "relative_path": "_DSC0007.jpg",
        "width": 6000,
        "height": 4000,
        "format": "jpeg",
        "camera_make": "SONY",
        "blur_score": 44.54,
        "contrast": 11.80,
        "e6_score": 100,
    }
    blurry = evaluate_local_quality(observed_blurry_frame)
    assert blurry["decision"] == "auto_excluded"
    assert blurry["primary_reason"] == "severe_blur"
    assert local_candidate_score(observed_blurry_frame, evaluation=blurry) == 0

    clear_frame = {**observed_blurry_frame, "blur_score": 324.05, "contrast": 26.70}
    clear = evaluate_local_quality(clear_frame)
    assert clear["decision"] == "pass"
    assert local_candidate_score(clear_frame, evaluation=clear) > 0
