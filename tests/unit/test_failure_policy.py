from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from inktime.app.domain.jobs.failure_policy import (
    FailureClass,
    JobConfigurationError,
    NoContentError,
    NoEligibleCandidatesError,
    classify_codes,
    classify_failure,
)


def test_terminal_business_outcomes_never_use_retry_policy():
    assert classify_failure(NoContentError("empty")) == FailureClass.TERMINAL_NO_RETRY
    assert classify_failure(NoEligibleCandidatesError("none")) == FailureClass.TERMINAL_NO_RETRY
    assert classify_failure(JobConfigurationError("bad")) == FailureClass.TERMINAL_NO_RETRY
    assert classify_codes(["NO_CONTENT", "CONFIG_INVALID"]) == FailureClass.TERMINAL_NO_RETRY
    assert classify_codes(["VLM-004", "VLM-006", "VLM-AMBIGUOUS"]) == FailureClass.TERMINAL_NO_RETRY


def test_transient_and_stale_outcomes_remain_distinct():
    assert classify_codes(["JOB-004"]) == FailureClass.RETRYABLE
    assert classify_codes(["LEASE_EXPIRED"]) == FailureClass.STALE_RECOVERY
    assert classify_codes(["NO_CONTENT", "JOB-004"]) == FailureClass.RETRYABLE


def test_terminal_schedule_cursor_uses_next_normal_cron_slot(app):
    repository = app.extensions["inktime_schedule_repository"]
    task = repository.get("display_prepare")
    assert task is not None
    now = datetime(2026, 8, 6, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    repository.record_terminal(task, "NO_CONTENT empty", now)
    updated = repository.get("display_prepare")
    assert updated is not None
    next_run = datetime.fromisoformat(str(updated["next_run"]))
    assert next_run > now
    assert (next_run - now).total_seconds() > int(task["retry_interval_seconds"])


def test_structured_terminal_outcome_clears_retry_state(app):
    repository = app.extensions["inktime_schedule_repository"]
    task = repository.get("display_prepare")
    assert task is not None
    now = datetime(2026, 8, 6, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    repository.record_failure(task, "temporary retry", now)
    repository.record_terminal_outcome(task, "NO_CONTENT", now)
    updated = repository.get("display_prepare")
    assert updated is not None
    assert updated["last_failure"] is None
    assert updated["error_status"] is None
    next_run = datetime.fromisoformat(str(updated["next_run"]))
    assert next_run > now
    assert (next_run - now).total_seconds() > int(task["retry_interval_seconds"])
