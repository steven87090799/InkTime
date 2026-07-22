from __future__ import annotations

import pytest


class FirstChoice:
    def choice(self, values):
        return values[0]

    def random(self):
        return 0.0


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("random", "travel"),
        ("weighted", "travel"),
        ("top_n", "person"),
        ("prefer_unseen", "travel"),
        ("prefer_travel", "travel"),
        ("prefer_person", "person"),
    ],
)
def test_same_day_reroll_modes_are_bounded_and_do_not_fallback_dates(
    app, monkeypatch, mode, expected
):
    service = app.extensions["inktime_render_service"]
    rows = [
        {
            "id": "person",
            "captured_at": "2019-07-22T10:00:00",
            "final_score": 95,
            "types": ["人物"],
        },
        {
            "id": "travel",
            "captured_at": "2020-07-22T10:00:00",
            "final_score": 80,
            "types": ["旅行"],
        },
    ]
    monkeypatch.setattr(service, "_iter_history_rows", lambda *args, **kwargs: iter(rows))
    monkeypatch.setattr(service, "_was_displayed", lambda photo_id: photo_id == "person")

    result = service.reroll_history_day(
        {"month_day": "07-22", "mode": mode, "top_n": 1}, rng=FirstChoice()
    )

    assert result["status"] == "ok"
    assert result["month_day"] == "07-22"
    assert result["candidates"][0]["id"] == expected
