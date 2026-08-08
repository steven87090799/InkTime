"""Job-domain contracts shared by the scheduler and background workers."""

from .failure_policy import (
    FailureClass,
    JobConfigurationError,
    NoContentError,
    NoEligibleCandidatesError,
    classify_failure,
    classify_codes,
    failure_code,
)

__all__ = [
    "FailureClass",
    "JobConfigurationError",
    "NoContentError",
    "NoEligibleCandidatesError",
    "classify_failure",
    "classify_codes",
    "failure_code",
]
