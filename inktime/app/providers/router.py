from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
import threading
import time

from .base import ProviderResponse, Usage, VisionProvider
from .openai_compatible import ProviderHTTPError
from inktime.app.core.logging import log_event, should_log_rate_limited


LOGGER = logging.getLogger("provider_router")


def _log_debug(message: str, *, event: str, **fields) -> None:
    if not LOGGER.isEnabledFor(logging.DEBUG):
        return
    key = ":".join(
        (
            "provider-router",
            event,
            str(fields.get("provider") or "route")[:64],
            str(fields.get("operation") or "unknown")[:64],
        )
    )
    if should_log_rate_limited(key, interval_seconds=1):
        log_event(LOGGER, logging.DEBUG, message, event=event, **fields)


@dataclass
class ProviderChannel:
    provider: VisionProvider
    priority: int = 100
    max_concurrency: int = 2
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    cooldown_seconds: int = 300
    semaphore: threading.BoundedSemaphore = field(init=False)
    request_times: deque = field(default_factory=deque)
    token_events: deque = field(default_factory=deque)
    failures: int = 0
    circuit_until: float = 0

    def __post_init__(self):
        self.semaphore = threading.BoundedSemaphore(max(1, self.max_concurrency))


class FailoverVisionProvider(VisionProvider):
    def __init__(self, channels: list[ProviderChannel], failure_threshold: int = 3) -> None:
        if not channels:
            raise ValueError("沒有可用 Provider")
        self.channels = sorted(channels, key=lambda channel: channel.priority)
        self.failure_threshold = failure_threshold
        self._lock = threading.Lock()
        self._local = threading.local()

    @property
    def name(self) -> str:
        channel = getattr(self._local, "channel", None)
        return channel.provider.name if channel else "Provider Router"

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    def _available(self, channel: ProviderChannel) -> bool:
        now = time.monotonic()
        with self._lock:
            while channel.request_times and channel.request_times[0] <= now - 60:
                channel.request_times.popleft()
            while channel.token_events and channel.token_events[0][0] <= now - 60:
                channel.token_events.popleft()
            if channel.circuit_until > now:
                return False
            if channel.requests_per_minute and len(channel.request_times) >= channel.requests_per_minute:
                return False
            if (
                channel.tokens_per_minute
                and sum(event[1] for event in channel.token_events) >= channel.tokens_per_minute
            ):
                return False
            channel.request_times.append(now)
            return True

    def _execute(self, method: str, **kwargs) -> ProviderResponse:
        last_error: Exception | None = None
        _log_debug(
            "Provider route started",
            event="provider_route_started",
            operation=method,
            model=str(kwargs.get("model") or ""),
            stage=str(kwargs.get("stage") or ""),
            details={"candidate_count": len(self.channels)},
        )
        for channel in self.channels:
            _log_debug(
                "Provider candidate evaluated",
                event="provider_candidate_evaluated",
                provider=channel.provider.name,
                operation=method,
                model=str(kwargs.get("model") or ""),
                details={"priority": channel.priority, "failures": channel.failures},
            )
            if not self._available(channel):
                _log_debug(
                    "Provider candidate unavailable or cooling down",
                    event="provider_candidate_cooldown",
                    provider=channel.provider.name,
                    operation=method,
                    details={"failures": channel.failures},
                )
                continue
            if not channel.semaphore.acquire(blocking=False):
                _log_debug(
                    "Provider candidate skipped because concurrency is full",
                    event="provider_candidate_skipped",
                    provider=channel.provider.name,
                    operation=method,
                    details={"reason": "concurrency_full"},
                )
                continue
            try:
                response = getattr(channel.provider, method)(**kwargs)
            except Exception as exc:
                last_error = exc
                with self._lock:
                    channel.failures += 1
                    retry_after = getattr(exc, "retry_after", None)
                    if channel.failures >= self.failure_threshold or retry_after:
                        channel.circuit_until = time.monotonic() + max(
                            float(retry_after or 0), channel.cooldown_seconds
                        )
                if should_log_rate_limited(
                    f"provider-failover:{channel.provider.name}:{method}",
                    interval_seconds=5,
                ):
                    log_event(
                        LOGGER,
                        logging.WARNING,
                        "Provider candidate failed; failover will continue",
                        event="provider_failover_started",
                        error_code=str(getattr(exc, "code", "VLM-005")),
                        provider=channel.provider.name,
                        operation=method,
                        failure_class=type(exc).__name__,
                        retryable=True,
                        ambiguous=bool(getattr(exc, "ambiguous", False)),
                        details={"failures": channel.failures},
                    )
                continue
            finally:
                channel.semaphore.release()
            with self._lock:
                channel.failures = 0
                used_tokens = response.usage.input_tokens + response.usage.output_tokens
                if used_tokens:
                    channel.token_events.append((time.monotonic(), used_tokens))
            self._local.channel = channel
            _log_debug(
                "Provider candidate selected",
                event="provider_candidate_selected",
                provider=channel.provider.name,
                operation=method,
                model=str(kwargs.get("model") or ""),
                details={"priority": channel.priority},
            )
            return response
        if last_error:
            if should_log_rate_limited(
                f"provider-route-exhausted:{method}", interval_seconds=5
            ):
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "Provider route exhausted",
                    event="provider_route_exhausted",
                    error_code=str(getattr(last_error, "code", "VLM-005")),
                    operation=method,
                    failure_class=type(last_error).__name__,
                    retryable=False,
                )
            raise last_error
        if should_log_rate_limited(
            f"provider-route-unavailable:{method}", interval_seconds=5
        ):
            log_event(
                LOGGER,
                logging.ERROR,
                "Provider route exhausted without an available candidate",
                event="provider_route_exhausted",
                error_code="VLM-005",
                operation=method,
                retryable=True,
            )
        raise ProviderHTTPError("所有 Provider 暫時不可用或已達 Rate Limit", "VLM-005")

    def analyze(self, **kwargs) -> ProviderResponse:
        return self._execute("analyze", **kwargs)

    def repair_json(self, **kwargs) -> ProviderResponse:
        channel = getattr(self._local, "channel", None)
        if channel is None:
            return self._execute("repair_json", **kwargs)
        return channel.provider.repair_json(**kwargs)

    def submit_batch(self, requests, completion_window="24h") -> str:
        last_error: Exception | None = None
        for channel in self.channels:
            try:
                result = channel.provider.submit_batch(requests, completion_window=completion_window)
                self._local.channel = channel
                return result
            except Exception as exc:
                last_error = exc
                if should_log_rate_limited(
                    f"batch-provider-failover:{channel.provider.name}",
                    interval_seconds=5,
                ):
                    log_event(
                        LOGGER,
                        logging.WARNING,
                        "Batch provider submission failed; failover will continue",
                        event="batch_provider_failover",
                        error_code=str(getattr(exc, "code", "VLM-007")),
                        provider=channel.provider.name,
                        operation="batch_submit",
                        failure_class=type(exc).__name__,
                        retryable=True,
                        ambiguous=bool(getattr(exc, "ambiguous", False)),
                    )
                continue
        error = ProviderHTTPError("所有 Provider 的 Batch 提交均失敗", "VLM-007")
        raise error from last_error

    def poll_batch(self, batch_id: str) -> dict:
        channel = getattr(self._local, "channel", self.channels[0])
        return channel.provider.poll_batch(batch_id)

    def cancel_batch(self, batch_id: str) -> dict:
        channel = getattr(self._local, "channel", self.channels[0])
        return channel.provider.cancel_batch(batch_id)

    def estimate_cost(self, model: str, usage: Usage) -> float:
        channel = getattr(self._local, "channel", self.channels[0])
        return channel.provider.estimate_cost(model, usage)

    def validate_config(self) -> tuple[bool, str]:
        results = [channel.provider.validate_config() for channel in self.channels]
        return (any(result[0] for result in results), "；".join(result[1] for result in results))
