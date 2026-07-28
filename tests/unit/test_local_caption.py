from __future__ import annotations

from inktime.app.domain.rendering.local_caption import build_local_caption


def test_local_caption_is_photo_scoped_and_never_marks_local_text_as_ai():
    first = build_local_caption(
        photo_id="a", captured_at="2021-07-28T10:00:00+00:00", display_date="2026-07-28",
        timezone="Asia/Taipei", known_location="台中", maximum_characters=16,
    )
    second = build_local_caption(
        photo_id="b", captured_at="2020-01-02T10:00:00+00:00", display_date="2026-07-28",
        timezone="Asia/Taipei", known_location="", maximum_characters=16,
    )
    assert first["photo_id"] == "a" and second["photo_id"] == "b"
    assert first["text"] != second["text"]
    assert first["is_ai_generated"] is False and second["is_ai_generated"] is False


def test_existing_photo_caption_is_preserved_only_for_its_photo():
    caption = build_local_caption(
        photo_id="a", captured_at=None, display_date="2026-07-28", timezone="Asia/Taipei",
        existing_side_caption="照片 A 的既有句子", maximum_characters=30,
    )
    assert caption["text"] == "照片 A 的既有句子"
    assert caption["source"] == "existing_ai_side_caption"
    assert caption["is_ai_generated"] is True


def test_caption_uses_requested_timezone_for_historical_today():
    taipei = build_local_caption(
        photo_id="a", captured_at="2020-07-27T17:30:00Z", display_date="2026-07-28",
        timezone="Asia/Taipei",
    )
    los_angeles = build_local_caption(
        photo_id="a", captured_at="2020-07-27T17:30:00Z", display_date="2026-07-28",
        timezone="America/Los_Angeles",
    )
    assert taipei["source"] == "local_historical_today"
    assert los_angeles["source"] != "local_historical_today"


def test_invalid_timezone_uses_safe_utc_fallback():
    caption = build_local_caption(
        photo_id="a", captured_at="2020-07-27T17:30:00Z", display_date="2026-07-27",
        timezone="not/a/timezone",
    )
    assert caption["source"] == "local_historical_today"
