from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from inktime.app.domain.jobs.failure_policy import NoContentError
from inktime.app.services.display_prepare import DisplayPrepareConfig, DisplayPreparationService


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


def test_schedule_preview_template_selects_multi_device_without_reselection():
    template = (
        Path(__file__).resolve().parents[2] / "inktime/app/web/templates/schedules.html"
    ).read_text(encoding="utf-8")

    assert "choosePreviewDevice" in template
    assert "window.prompt" in template
    assert "device_id:selectedDeviceId" in template
    assert "playlistPreviewText" in template


class EmptyRenderService:
    settings = {}

    @staticmethod
    def _today():
        return date(2026, 8, 6)

    @staticmethod
    def select_candidates_details(*_args, **_kwargs):
        return []


@pytest.mark.parametrize("fallback", ["use_existing", "skip"])
def test_empty_display_prepare_returns_structured_no_content(fallback):
    service = DisplayPreparationService(None, EmptyRenderService())

    result = service.prepare({"ai_fallback": fallback}, created_by="tester")

    assert result["status"] == "completed"
    assert result["outcome_code"] == "NO_CONTENT"
    assert result["output_count"] == 0


def test_empty_display_prepare_fail_fallback_remains_explicit_terminal_error():
    service = DisplayPreparationService(None, EmptyRenderService())

    with pytest.raises(NoContentError):
        service.prepare({"ai_fallback": "fail"}, created_by="tester")


def test_display_prepare_rejects_unknown_or_silently_ignored_fields():
    with pytest.raises(ValueError, match="JSON 物件"):
        DisplayPrepareConfig.from_mapping([])
    with pytest.raises(ValueError, match="不支援"):
        DisplayPrepareConfig.from_mapping({"display_times": ["08:00"], "ignored": True})
    with pytest.raises(ValueError, match="不得少於"):
        DisplayPrepareConfig.from_mapping({"display_times": ["08:00"], "daily_count": 2})


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"display_times": "08:00"}, "非空陣列"),
        ({"display_times": []}, "非空陣列"),
        ({"display_times": ["not-a-clock"]}, "HH:MM"),
        ({"display_times": ["08:00"], "lead_minutes": True}, "必須是整數"),
        ({"display_times": ["08:00"], "lead_minutes": -1}, "介於"),
        ({"display_times": ["08:00"], "prefetch_count": 0}, "介於"),
        ({"display_times": ["08:00"], "device_ids": [1]}, "裝置 ID"),
        ({"display_times": ["08:00"], "candidate_years": "2020"}, "年份陣列"),
        ({"display_times": ["08:00"], "candidate_years": ["2020"]}, "年份陣列"),
        ({"display_times": ["08:00"], "candidate_years": [1899]}, "超出"),
        ({"display_times": ["08:00"], "ai_fallback": "unknown"}, "ai_fallback"),
        ({"display_times": ["08:00"], "render_fallback": "unknown"}, "render_fallback"),
    ],
)
def test_display_prepare_rejects_invalid_supported_values(raw, message):
    with pytest.raises(ValueError, match=message):
        DisplayPrepareConfig.from_mapping(raw)


@pytest.mark.parametrize(
    ("result", "device_id", "expected"),
    [
        ({"device_releases": {"device-a": "assigned-release"}}, "device-a", "assigned-release"),
        ({"releases": [{"release_id": "single-release"}]}, "device-a", "single-release"),
        ({"id": "direct-release"}, "device-a", "direct-release"),
    ],
)
def test_offline_release_id_accepts_only_explicit_result_contracts(result, device_id, expected):
    assert DisplayPreparationService._offline_release_id(result, device_id) == expected


def test_offline_release_id_rejects_ambiguous_result():
    with pytest.raises(ValueError, match="Release ID"):
        DisplayPreparationService._offline_release_id({"releases": [{"id": "one"}, {"id": "two"}]}, "device-a")
