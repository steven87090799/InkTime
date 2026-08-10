"""Deterministic failure classes for queued work and scheduled triggers.

The scheduler must not infer retry behaviour from human-readable messages.
Every path that crosses the queue boundary therefore exposes a stable code;
unknown failures remain retryable so existing transient behaviour stays
conservative, while the explicit terminal set can never create a retry storm.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class FailureClass(str, Enum):
    TERMINAL_NO_RETRY = "terminal_no_retry"
    RETRYABLE = "retryable"
    STALE_RECOVERY = "stale_recovery"


# These are the public queue outcomes.  Existing stable InkTime codes are
# retained in the same set so older jobs receive the new policy as they finish.
TERMINAL_NO_RETRY_CODES = frozenset(
    {
        "NO_PHOTOS",
        "NO_CONTENT",
        "NO_ELIGIBLE_CANDIDATES",
        "CONFIG_INVALID",
        "AUTH_REQUIRED",
        "ANALYSIS_DISABLED",
        "UNSUPPORTED_CONFIGURATION",
        "ANALYSIS-DISABLED",
        "VLM-008",
        "VLM-004",
        "VLM-006",
        "VLM-AMBIGUOUS",
        "JOB-SHUTDOWN-AMBIGUOUS",
    }
)

RETRYABLE_CODES = frozenset(
    {
        "TRANSIENT",
        "TEMPORARY_IO_ERROR",
        "PROVIDER_TIMEOUT",
        "AI-PROVIDER-TIMEOUT",
        "AI-PROVIDER-UNAVAILABLE",
        "JOB-003",
        "JOB-004",
        "DISPLAY-005",
    }
)

STALE_RECOVERY_CODES = frozenset(
    {
        "STALE_RECOVERY",
        "LEASE_EXPIRED",
        "WORKER_CRASH",
    }
)


class JobFailure(RuntimeError):
    """Base class carrying a stable queue error code."""

    code = "JOB-003"
    failure_class = FailureClass.RETRYABLE

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code:
            self.code = str(code)
        super().__init__(message)


class JobShutdownAmbiguousError(JobFailure):
    """A running item outlived the worker shutdown fence; never retry it."""

    code = "JOB-SHUTDOWN-AMBIGUOUS"
    failure_class = FailureClass.TERMINAL_NO_RETRY


class JobConfigurationError(ValueError):
    code = "CONFIG_INVALID"
    failure_class = FailureClass.TERMINAL_NO_RETRY


class NoContentError(ValueError):
    code = "NO_CONTENT"
    failure_class = FailureClass.TERMINAL_NO_RETRY


class NoEligibleCandidatesError(ValueError):
    code = "NO_ELIGIBLE_CANDIDATES"
    failure_class = FailureClass.TERMINAL_NO_RETRY


def failure_code(value: object) -> str:
    """Return only a stable code; never parse a diagnostic message."""

    code = getattr(value, "code", None)
    if code is None and isinstance(value, str):
        code = value
    normalized = str(code or "JOB-003").strip()
    return normalized[:128] or "JOB-003"


def classify_failure(value: object) -> FailureClass:
    explicit = getattr(value, "failure_class", None)
    if isinstance(explicit, FailureClass):
        return explicit
    code = failure_code(value)
    if code in TERMINAL_NO_RETRY_CODES:
        return FailureClass.TERMINAL_NO_RETRY
    if code in STALE_RECOVERY_CODES:
        return FailureClass.STALE_RECOVERY
    if code in RETRYABLE_CODES:
        return FailureClass.RETRYABLE
    # Unknown failures use the bounded transient path.  This is deliberately
    # fail-safe for availability, while terminal business outcomes are always
    # represented by one of the explicit codes above.
    return FailureClass.RETRYABLE


def classify_codes(codes: Iterable[object]) -> FailureClass:
    """Classify a completed-with-errors job without inspecting messages."""

    normalized = [failure_code(code) for code in codes if failure_code(code)]
    if not normalized:
        return FailureClass.RETRYABLE
    classes = {classify_failure(code) for code in normalized}
    if FailureClass.RETRYABLE in classes:
        return FailureClass.RETRYABLE
    if FailureClass.STALE_RECOVERY in classes:
        return FailureClass.STALE_RECOVERY
    return FailureClass.TERMINAL_NO_RETRY
