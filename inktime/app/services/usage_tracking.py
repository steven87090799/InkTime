"""Shared usage accounting for provider requests with unknown outcomes."""

from __future__ import annotations

import time
from typing import Any


def record_failed_unknown_usage(
    usage_repository,
    *,
    provider: object,
    model: str,
    job_id: str | None,
    photo_id: str | None,
    request_type: str,
    started_at: str,
    started_perf: float,
    error: Exception,
    request_metrics: dict[str, Any] | None = None,
    request_id: str | None = None,
    error_code: str | None = None,
    retry_count: int = 0,
    image_bytes: int = 0,
) -> int:
    """Persist one sent/ambiguous request as unknown-cost evidence.

    The caller decides whether the transport was actually handed the request
    to the provider.  This helper deliberately records no token or cost
    estimate: a failed request must remain visible to the budget gate instead
    of being mistaken for a free, successfully measured call.
    """

    metrics = dict(request_metrics or {})
    try:
        metrics_image_bytes = int(metrics.get("image_bytes", 0) or 0)
    except (TypeError, ValueError):
        metrics_image_bytes = 0
    metrics["image_bytes"] = max(metrics_image_bytes, int(image_bytes or 0))
    provider_name = str(getattr(provider, "name", provider.__class__.__name__))
    provider_id = str(getattr(provider, "provider_id", provider_name))
    effective_request_id = request_id or getattr(error, "request_id", None)
    response_info = getattr(error, "response_info", None)
    if not effective_request_id and isinstance(response_info, dict):
        effective_request_id = response_info.get("request_id")
    return usage_repository.record(
        provider=provider_name,
        provider_id=provider_id,
        model=model,
        job_id=job_id,
        photo_id=photo_id,
        request_type=request_type,
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        estimated_cost=None,
        actual_cost=None,
        started_at=started_at,
        latency_ms=int((time.perf_counter() - started_perf) * 1000),
        status="failed",
        retry_count=retry_count,
        error_code=str(error_code or getattr(error, "code", "") or error.__class__.__name__)[:128],
        request_id=str(effective_request_id)[:255] if effective_request_id else None,
        reasoning_tokens=0,
        cache_write_tokens=0,
        cost_source="unknown",
        prompt_chars=metrics.get("prompt_chars", 0),
        schema_chars=metrics.get("schema_chars", 0),
        request_body_bytes=metrics.get("request_body_bytes", 0),
        image_bytes=metrics.get("image_bytes", 0),
    )
