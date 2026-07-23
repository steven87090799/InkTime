from __future__ import annotations

import pytest

from inktime.app.services.display_prepare import DisplayPrepareConfig


def test_display_prepare_consumes_every_supported_field():
    config = DisplayPrepareConfig.from_mapping(
        {
            "display_times": ["08:00", "18:00"],
            "lead_minutes": 45,
            "daily_count": 2,
            "device_ids": ["one", "two"],
            "candidate_years": [2018, 2020],
            "prefetch_count": 3,
            "ai_fallback": "skip",
            "render_fallback": "fail",
        }
    )
    assert config.output_count == 6
    assert config.device_ids == ("one", "two")
    assert config.candidate_years == (2018, 2020)
    assert config.preparation_times(__import__("datetime").date(2026, 7, 22))[0].endswith("07:15:00")


def test_display_prepare_rejects_unknown_or_silently_ignored_fields():
    with pytest.raises(ValueError, match="不支援"):
        DisplayPrepareConfig.from_mapping({"display_times": ["08:00"], "ignored": True})
    with pytest.raises(ValueError, match="不得少於"):
        DisplayPrepareConfig.from_mapping({"display_times": ["08:00"], "daily_count": 2})
