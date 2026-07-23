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
    assert select_pair_candidate(primary, [landscape, portrait], frame_orientation="landscape")["id"] == "portrait"


def test_portrait_frame_requires_a_landscape_partner():
    primary = _photo("primary", width=1600, height=900)
    portrait = _photo("portrait")
    landscape = _photo("landscape", width=1600, height=900)
    assert select_pair_candidate(primary, [portrait, landscape], frame_orientation="portrait")["id"] == "landscape"


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
