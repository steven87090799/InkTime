from __future__ import annotations

import json
from datetime import date

import pytest

from tests.conftest import create_admin
from tests.security.test_device_release_authorization import certified_release_metadata
from tests.unit.test_analysis_schema import content_filter_result, valid_result


def _photo(app):
    metadata = certified_release_metadata(app)
    return metadata["render_plans"][0]["primary_photo_id"]


def _save(app, photo_id, **changes):
    result = valid_result(**changes)
    app.extensions["inktime_photo_repository"].save_analysis(
        photo_id, None, "single", "safety-test", "test-model", result,
        json.dumps(result, ensure_ascii=False),
    )


@pytest.mark.parametrize("category", ["adult_media_cover", "commercial_model_photo", "screenshot", "document_record"])
@pytest.mark.parametrize("confidence,status", [(0.97, "auto_excluded"), (0.4, "pending_review")])
def test_display_categories_hold_delivery_and_only_uncertainty_can_be_reviewed(app, category, confidence, status):
    actor = create_admin(app)
    photo_id = _photo(app)
    _save(app, photo_id, display_exclusion={"category": category, "confidence": confidence})
    photos = app.extensions["inktime_photo_repository"]
    candidates = app.extensions["inktime_render_candidate_repository"]
    reviews = app.extensions["inktime_review_repository"]
    photo = photos.get_with_path(photo_id)
    assert photo["eligible"] == 0 and photo["exclusion_status"] == status
    assert candidates.get(photo_id) is None
    review = reviews.get(photo_id)
    payload = {"review_state": "keep", "analysis_id": review["analysis_id"]}
    if status == "auto_excluded":
        with pytest.raises(ValueError, match="v6 安全分析已排除"):
            reviews.update(photo_id, payload, actor_id=actor, expected_version=review["review_version"])
    else:
        assert review["review_state"] == "needs_review"
        reviews.update(photo_id, payload, actor_id=actor, expected_version=review["review_version"])
        assert candidates.get(photo_id) is not None


@pytest.mark.parametrize("action", ["restore", "delete_skip", "delete_never_show"])
def test_legacy_feedback_cannot_resolve_an_uncertain_negative(app, action):
    actor = create_admin(app)
    photo_id = _photo(app)
    _save(app, photo_id, content_filter=content_filter_result(
        explicit_nudity={"detected": False, "confidence": 0.2},
    ))
    photos = app.extensions["inktime_photo_repository"]
    feedback = app.extensions["inktime_resilience_repository"]
    assert photos.get_with_path(photo_id)["exclusion_status"] == "pending_review"
    if action == "restore":
        with pytest.raises(ValueError, match="人工審核"):
            feedback.submit_feedback(user_id=actor, payload={"photo_id": photo_id, "feedback_type": "RESTORE"})
    else:
        kind = "SKIP_TEMPORARILY" if action == "delete_skip" else "NEVER_SHOW"
        result = feedback.submit_feedback(user_id=actor, payload={"photo_id": photo_id, "feedback_type": kind})
        assert feedback.delete_feedback(result["feedback_id"]) is True
    assert photos.get_with_path(photo_id)["eligible"] == 0
    assert photos.get_with_path(photo_id)["exclusion_status"] == "pending_review"
    assert app.extensions["inktime_render_candidate_repository"].get(photo_id) is None


def test_clearing_a_confirmed_angle_reopens_the_orientation_hold(app):
    actor = create_admin(app)
    photo_id = _photo(app)
    _save(app, photo_id, visual_orientation={
        "rotation_cw": 90, "confidence": 0.8, "ambiguous": False, "evidence": ["faces_upright"],
    })
    photos = app.extensions["inktime_photo_repository"]
    reviews = app.extensions["inktime_review_repository"]
    candidates = app.extensions["inktime_render_candidate_repository"]
    review = reviews.get(photo_id)
    payload = {"review_state": "keep", "analysis_id": review["analysis_id"]}
    with pytest.raises(ValueError, match="先確認照片方向"):
        reviews.update(photo_id, payload, actor_id=actor, expected_version=review["review_version"])
    photos.set_manual_orientation(photo_id, 90, actor)
    reviews.update(photo_id, payload, actor_id=actor, expected_version=review["review_version"])
    assert candidates.get(photo_id) is not None
    photos.set_manual_orientation(photo_id, None, actor)
    photo = photos.get_with_path(photo_id)
    assert photo["exclusion_status"] == "pending_review" and photo["eligible"] == 0
    assert reviews.get(photo_id)["review_state"] == "needs_review"
    assert candidates.get(photo_id) is None


def test_feedback_restore_still_requires_direction_after_a_review_exclusion(app):
    actor = create_admin(app)
    photo_id = _photo(app)
    _save(app, photo_id, visual_orientation={
        "rotation_cw": 90, "confidence": 0.8, "ambiguous": False, "evidence": ["faces_upright"],
    })
    reviews = app.extensions["inktime_review_repository"]
    review = reviews.get(photo_id)
    reviews.update(
        photo_id, {"review_state": "exclude", "analysis_id": review["analysis_id"]},
        actor_id=actor, expected_version=review["review_version"],
    )
    with pytest.raises(ValueError, match="確認方向"):
        app.extensions["inktime_resilience_repository"].submit_feedback(
            user_id=actor, payload={"photo_id": photo_id, "feedback_type": "RESTORE"},
        )


def test_identical_cached_evidence_keeps_human_approval_and_changed_risk_reopens_review(app):
    actor = create_admin(app)
    photo_id = _photo(app)
    changes = {"display_exclusion": {"category": "none", "confidence": 0.4}}
    _save(app, photo_id, **changes)
    reviews = app.extensions["inktime_review_repository"]
    review = reviews.get(photo_id)
    reviews.update(
        photo_id, {"review_state": "keep", "analysis_id": review["analysis_id"]},
        actor_id=actor, expected_version=review["review_version"],
    )
    _save(app, photo_id, **changes)
    assert reviews.get(photo_id)["review_state"] == "keep"
    assert app.extensions["inktime_render_candidate_repository"].get(photo_id) is not None
    _save(app, photo_id, display_exclusion={"category": "commercial_model_photo", "confidence": 0.4})
    assert reviews.get(photo_id)["review_state"] == "needs_review"
    assert app.extensions["inktime_render_candidate_repository"].get(photo_id) is None
    with app.extensions["inktime_database"].session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0


@pytest.mark.parametrize("risk", ["content", "orientation"])
def test_old_eligible_projection_cannot_bypass_current_safety_evidence(app, risk):
    photo_id = _photo(app)
    if risk == "content":
        _save(app, photo_id, content_filter=content_filter_result(
            explicit_nudity={"detected": False, "confidence": 0.2},
        ))
    else:
        _save(app, photo_id, visual_orientation={
            "rotation_cw": 90, "confidence": 0.8, "ambiguous": False, "evidence": ["faces_upright"],
        })
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute("UPDATE photos SET eligible=1,exclusion_status='eligible' WHERE id=?", (photo_id,))
    assert app.extensions["inktime_render_candidate_repository"].get(photo_id) is None
    assert app.extensions["inktime_render_service"]._candidate_query(
        target=date(2026, 10, 21), month_days=None, older_only=False, limit=10,
    ) == []
