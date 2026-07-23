from __future__ import annotations

import pytest

from inktime.app.domain.rendering.adaptive_layout import (
    decide_adaptive_layout,
    retained_ratio,
    select_pair_candidate,
)


def test_landscape_frame_uses_single_for_16_by_9_and_pair_for_9_by_16():
    area = (800, 404)  # 800x480 minus the existing 76px Footer
    assert decide_adaptive_layout((1600, 900), area).mode == "single"
    assert decide_adaptive_layout((900, 1600), area).mode == "pair"


def test_portrait_frame_uses_single_for_9_by_16_and_pair_for_16_by_9():
    area = (480, 704)  # 480x800 minus the existing 96px Footer
    assert decide_adaptive_layout((900, 1600), area).mode == "single"
    assert decide_adaptive_layout((1600, 900), area).mode == "pair"


def test_near_square_and_exact_threshold_are_calculated_not_classified():
    assert decide_adaptive_layout((1000, 1000), (800, 404)).mode == "pair"
    assert retained_ratio((78, 100), (100, 100)) == pytest.approx(0.78)
    assert decide_adaptive_layout((78, 100), (100, 100)).mode == "single"


def test_subject_lost_by_cover_requests_pair_even_when_area_is_high():
    decision = decide_adaptive_layout(
        (1200, 1000),
        (800, 800),
        subject_box=(0.0, 0.2, 0.18, 0.8),
        focus_x=0.9,
    )
    assert decision.retained_ratio >= 0.78
    assert decision.subject_retained_ratio < 0.9
    assert decision.mode == "pair"


def _photo(photo_id: str, **extra):
    return {
        "id": photo_id,
        "width": 900,
        "height": 1600,
        "captured_at": "2024-07-01T10:00:00+00:00",
        "gps_lat": 25.03,
        "gps_lon": 121.56,
        "city": "臺北市",
        "types": ["人物"],
        "sha256": f"sha-{photo_id}",
        "duplicate_group_id": None,
        "perceptual_hash": f"phash-{photo_id}",
        "difference_hash": f"dhash-{photo_id}",
        "ever_displayed": False,
        "recently_displayed": False,
        **extra,
    }


def test_pairing_prefers_same_day_then_nearer_time_and_location():
    primary = _photo("primary")
    farther = _photo("farther", captured_at="2024-07-01T16:00:00+00:00", gps_lat=24.0)
    closest = _photo("closest", captured_at="2024-07-01T10:30:00+00:00")
    assert select_pair_candidate(primary, [farther, closest], frame_orientation="landscape")["id"] == "closest"


def test_pairing_excludes_self_recent_and_near_duplicate_and_returns_none():
    primary = _photo("primary")
    self_photo = _photo("primary")
    recent = _photo("recent", recently_displayed=True)
    duplicate = _photo("duplicate", perceptual_hash=primary["perceptual_hash"])
    assert select_pair_candidate(primary, [self_photo, recent, duplicate], frame_orientation="landscape") is None


def test_pairing_does_not_fill_an_unrelated_photo_just_to_make_a_pair():
    primary = _photo("primary")
    unrelated = _photo(
        "unrelated", captured_at="2023-01-01T03:00:00+00:00", gps_lat=35.6,
        gps_lon=139.7, city="東京", types=["風景"], ever_displayed=True,
    )
    assert select_pair_candidate(primary, [unrelated], frame_orientation="landscape") is None


def test_portrait_frame_requires_landscape_partner():
    primary = _photo("primary", width=1600, height=900)
    portrait = _photo("portrait")
    landscape = _photo("landscape", width=1600, height=900)
    assert select_pair_candidate(primary, [portrait, landscape], frame_orientation="portrait")["id"] == "landscape"
