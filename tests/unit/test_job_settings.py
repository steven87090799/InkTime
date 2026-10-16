import pytest

from inktime.app.domain.jobs.settings import validate_worker_settings


@pytest.mark.parametrize("settings", [
    [], None, {"concurrency": "2"}, {"concurrency": True}, {"concurrency": {}},
    {"concurrency": 0}, {"concurrency": 9}, {"max_attempts": 0},
    {"max_attempts": 12}, {"max_retries": -1}, {"max_retries": 11},
    {"retry_interval_seconds": 0}, {"retry_interval_seconds": 86401},
    {"timeout_seconds": -1}, {"timeout_seconds": 86401}, {"timeout_seconds": 1.5},
    {"timeout_seconds": None},
])
def test_worker_settings_reject_unsafe_values(settings):
    with pytest.raises(ValueError, match="JOB-001"):
        validate_worker_settings(settings)


def test_worker_settings_keep_zero_retry_and_timeout_and_optional_backoff():
    settings = {
        "concurrency": 8, "max_attempts": 11, "max_retries": 0,
        "retry_interval_seconds": None, "timeout_seconds": 0,
        "operation": "cleanup",
    }
    assert validate_worker_settings(settings) == settings
    assert validate_worker_settings(settings) is not settings
