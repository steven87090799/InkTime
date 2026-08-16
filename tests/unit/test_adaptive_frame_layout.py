from __future__ import annotations

from inktime.app.domain.rendering.adaptive_layout import (
    dimensions_after_exif,
    photo_orientation,
    select_pair_candidate,
)


def test_orientation_classification_uses_square_band_and_exif_rotation():
    assert dimensions_after_exif(1600, 900, 6) == (900, 1600)
    assert photo_orientation((1600, 900)) == "landscape"
    assert photo_orientation((900, 1600)) == "portrait"
    assert photo_orientation((1000, 1050)) == "square"
    assert photo_orientation((900, 1000)) == "square"


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


def test_landscape_frame_requires_a_portrait_partner():
    primary = _photo("primary")
    portrait = _photo("portrait")
    landscape = _photo("landscape", width=1600, height=900)
    assert (
        select_pair_candidate(primary, [landscape, portrait], frame_orientation="landscape")["id"]
        == "portrait"
    )


def test_portrait_frame_requires_a_landscape_partner():
    primary = _photo("primary", width=1600, height=900)
    portrait = _photo("portrait")
    landscape = _photo("landscape", width=1600, height=900)
    assert (
        select_pair_candidate(primary, [portrait, landscape], frame_orientation="portrait")["id"]
        == "landscape"
    )


def test_pairing_prefers_same_day_then_nearer_time_and_location():
    primary = _photo("primary")
    farther = _photo("farther", captured_at="2024-07-01T16:00:00+00:00", gps_lat=24.0)
    closest = _photo("closest", captured_at="2024-07-01T10:30:00+00:00")
    assert (
        select_pair_candidate(primary, [farther, closest], frame_orientation="landscape")["id"] == "closest"
    )


def test_pairing_excludes_self_and_near_duplicate_and_returns_none():
    primary = _photo("primary")
    self_photo = _photo("primary")
    duplicate = _photo("duplicate", perceptual_hash=primary["perceptual_hash"])
    assert (
        select_pair_candidate(primary, [self_photo, duplicate], frame_orientation="landscape") is None
    )


def test_unrelated_recent_photo_can_pair_without_location_or_type_overlap():
    primary = _photo("primary", captured_at="2026-08-10T10:00:00+00:00")
    unrelated = _photo(
        "unrelated",
        captured_at="2026-08-12T03:00:00+00:00",
        gps_lat=35.6,
        gps_lon=139.7,
        city="東京",
        types=["風景"],
        ever_displayed=True,
    )
    assert select_pair_candidate(primary, [unrelated], frame_orientation="landscape") == unrelated


def test_pairing_date_priorities_cover_same_day_three_days_seven_days_and_any_date():
    primary = _photo("primary", captured_at="2026-08-10T10:00:00+00:00")
    same_day = _photo("same-day", captured_at="2026-08-10T22:00:00+00:00")
    within_three = _photo("three-day", captured_at="2026-08-12T10:00:00+00:00")
    within_seven = _photo("seven-day", captured_at="2026-08-16T10:00:00+00:00")
    any_date = _photo("any-date", captured_at="2025-01-01T10:00:00+00:00")

    assert select_pair_candidate(
        primary, [any_date, within_seven, within_three, same_day], frame_orientation="landscape"
    ) == same_day
    assert select_pair_candidate(
        primary, [any_date, within_seven, within_three], frame_orientation="landscape"
    ) == within_three
    assert select_pair_candidate(
        primary, [any_date, within_seven], frame_orientation="landscape"
    ) == within_seven
    assert select_pair_candidate(primary, [any_date], frame_orientation="landscape") == any_date


def test_recently_displayed_is_allowed_last_and_fresh_candidate_wins_inside_priority():
    primary = _photo("primary")
    recent = _photo("recent", captured_at="2024-07-01T10:05:00+00:00", recently_displayed=True)
    fresh = _photo("fresh", captured_at="2024-07-01T11:00:00+00:00")

    assert select_pair_candidate(primary, [recent], frame_orientation="landscape") == recent
    assert select_pair_candidate(primary, [recent, fresh], frame_orientation="landscape") == fresh


def test_all_duplicate_hash_contracts_remain_excluded():
    primary = _photo("primary", duplicate_group_id="group-primary")
    for key in ("sha256", "duplicate_group_id", "perceptual_hash", "difference_hash"):
        duplicate = _photo(f"duplicate-{key}", **{key: primary[key]})
        assert select_pair_candidate(primary, [duplicate], frame_orientation="landscape") is None

    exact_primary = _photo("exact-primary", library_id="library", relative_path="same.jpg")
    exact_copy = _photo("exact-copy", library_id="library", relative_path="same.jpg")
    assert select_pair_candidate(exact_primary, [exact_copy], frame_orientation="landscape") is None


def test_wrong_and_square_orientations_never_fill_a_pair():
    primary = _photo("primary")
    wrong = _photo("landscape", width=1600, height=900)
    square = _photo("square", width=1000, height=1000)
    assert select_pair_candidate(primary, [wrong, square], frame_orientation="landscape") is None


def test_pair_orientation_keeps_manual_and_high_confidence_visual_overrides():
    primary = _photo("primary")
    manual = _photo(
        "manual", width=1600, height=900, manual_orientation_rotation_cw=90
    )
    visual = _photo(
        "visual",
        width=1600,
        height=900,
        visual_orientation_rotation_cw=270,
        visual_orientation_confidence=0.99,
        visual_orientation_ambiguous=False,
    )
    assert select_pair_candidate(primary, [manual], frame_orientation="landscape") == manual
    assert select_pair_candidate(primary, [visual], frame_orientation="landscape") == visual
