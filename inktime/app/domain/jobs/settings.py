"""Pure validation shared by job creation and persisted worker inputs."""
from __future__ import annotations

from typing import Any


def validate_worker_settings(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("JOB-001 settings 必須是 JSON 物件")
    settings = dict(value)
    bounds = {
        "concurrency": (1, 8),
        "max_attempts": (1, 11),  # scheduled retry_count may be 10, plus the first attempt
        "max_retries": (0, 10),
        "retry_interval_seconds": (1, 86400),
        "timeout_seconds": (0, 86400),
    }
    for key, (minimum, maximum) in bounds.items():
        if key not in settings:
            continue
        number = settings[key]
        if key == "retry_interval_seconds" and number is None:
            continue  # keep the worker's bounded exponential-backoff default
        if type(number) is not int or not minimum <= number <= maximum:
            raise ValueError(f"JOB-001 settings.{key} 必須是 {minimum}–{maximum} 的 JSON 整數")
    return settings
