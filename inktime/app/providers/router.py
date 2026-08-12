from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import logging
import threading
import time

from inktime.app.core.logging import log_event, should_log_rate_limited
from .base import ProviderResponse, Usage, VisionProvider
from .openai_compatible import ProviderHTTPError


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

    @staticmethod
    def _prune_quota_events(channel: ProviderChannel, now: float) -> None:
        while channel.request_times and channel.request_times[0] <= now - 60:
            channel.request_times.popleft()
        while channel.token_events and channel.token_events[0][0] <= now - 60:
            channel.token_events.popleft()

    def _can_use_channel_locked(self, channel: ProviderChannel, now: float) -> bool:
        self._prune_quota_events(channel, now)
        if channel.circuit_until > now:
            return False
        if channel.requests_per_minute and len(channel.request_times) >= channel.requests_per_minute:
            return False
        if (
            channel.tokens_per_minute
            and sum(event[1] for event in channel.token_events) >= channel.tokens_per_minute
        ):
            return False
        return True

    def _available(self, channel: ProviderChannel) -> bool:
        """Inspect channel state without consuming an RPM slot."""

        with self._lock:
            return self._can_use_channel_locked(channel, time.monotonic())

    def _reserve_request_slot(self, channel: ProviderChannel) -> bool:
        """Consume RPM only after a semaphore permit is available."""

        with self._lock:
            now = time.monotonic()
            if not self._can_use_channel_locked(channel, now):
                return False
            channel.request_times.append(now)
            return True

    def candidate_channels(self, *, excluded: set[str] | None = None) -> list[ProviderChannel]:
        """Return identities that are currently eligible for network work.

        This remains the compatibility API for direct router callers.  It is
        deliberately *not* suitable for cache lookup because it filters
        circuit-, rate-, and token-limited channels.
        """
        excluded = excluded or set()
        now = time.monotonic()
        result: list[ProviderChannel] = []
        with self._lock:
            for channel in self.channels:
                while channel.request_times and channel.request_times[0] <= now - 60:
                    channel.request_times.popleft()
                while channel.token_events and channel.token_events[0][0] <= now - 60:
                    channel.token_events.popleft()
                provider_identity = str(getattr(channel.provider, "provider_id", channel.provider.name))
                if provider_identity in excluded or channel.circuit_until > now:
                    continue
                if channel.requests_per_minute and len(channel.request_times) >= channel.requests_per_minute:
                    continue
                if (
                    channel.tokens_per_minute
                    and sum(event[1] for event in channel.token_events) >= channel.tokens_per_minute
                ):
                    continue
                result.append(channel)
        return result

    def route_channels(self, *, excluded: set[str] | None = None) -> list[ProviderChannel]:
        """Return Frozen Route identities without inspecting network state.

        Cache identity is a configured provider identity, not a promise that a
        network request can currently be made.  This method neither consumes
        quotas nor acquires permits, and never filters a channel for an open
        circuit, RPM, TPM, or semaphore state.
        """
        excluded = excluded or set()
        with self._lock:
            return [
                channel
                for channel in self.channels
                if str(getattr(channel.provider, "provider_id", channel.provider.name)) not in excluded
            ]

    def acquire_channel(self, channel: ProviderChannel) -> bool:
        """Reserve RPM and network concurrency only for the cache owner."""
        if not channel.semaphore.acquire(blocking=False):
            return False
        if self._reserve_request_slot(channel):
            self._local.channel = channel
            return True
        channel.semaphore.release()
        return False

    def select_channel(self, *, excluded: set[str] | None = None) -> ProviderChannel:
        """Reserve the concrete provider that will own one cache key.

        Cache identity must never be the synthetic router name.  Keeping the
        reservation here also preserves the router's concurrency and circuit
        breaker guarantees for callers that need to inspect the provider before
        making a request.
        """
        excluded = excluded or set()
        for channel in self.channels:
            if str(getattr(channel.provider, "provider_id", channel.provider.name)) in excluded:
                continue
            if self.acquire_channel(channel):
                return channel
        raise ProviderHTTPError("所有 Provider 暫時不可用或已達 Rate Limit", "VLM-005")

    def release_channel(
        self,
        channel: ProviderChannel,
        *,
        usage: Usage | None = None,
        error: Exception | None = None,
    ) -> None:
        """Complete a reservation acquired by :meth:`select_channel`."""
        with self._lock:
            if error is not None:
                channel.failures += 1
                retry_after = getattr(error, "retry_after", None)
                if channel.failures >= self.failure_threshold or retry_after:
                    channel.circuit_until = time.monotonic() + max(
                        float(retry_after or 0), channel.cooldown_seconds
                    )
            else:
                channel.failures = 0
                if usage is not None:
                    used_tokens = usage.input_tokens + usage.output_tokens
                    if used_tokens:
                        channel.token_events.append((time.monotonic(), used_tokens))
        channel.semaphore.release()

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
            if not self.acquire_channel(channel):
                continue
            self._local.channel = channel
            try:
                response = getattr(channel.provider, method)(**kwargs)
            except Exception as exc:
                last_error = exc
                self.release_channel(channel, error=exc)
                if bool(getattr(exc, "vision_started", False)) or bool(
                    getattr(exc, "request_started", False)
                ) or bool(getattr(exc, "ambiguous", False)):
                    raise
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
                        provider_id=str(
                            getattr(channel.provider, "provider_id", channel.provider.name)
                        ),
                        operation=method,
                        failure_class=type(exc).__name__,
                        retryable=True,
                        ambiguous=False,
                    )
                continue
            self.release_channel(channel, usage=response.usage)
            _log_debug(
                "Provider candidate selected",
                event="provider_candidate_selected",
                provider=channel.provider.name,
                provider_id=str(getattr(channel.provider, "provider_id", channel.provider.name)),
                operation=method,
                model=str(kwargs.get("model") or ""),
                details={"priority": channel.priority},
            )
            return response
        if last_error:
            if should_log_rate_limited(f"provider-route-exhausted:{method}", interval_seconds=5):
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
        if should_log_rate_limited(f"provider-route-unavailable:{method}", interval_seconds=5):
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

    def _execute_sticky(
        self,
        channel: ProviderChannel,
        method: str,
        *,
        boundary=None,
        **kwargs,
    ) -> ProviderResponse:
        """Run one request on a selected channel without failover."""

        if not self.acquire_channel(channel):
            raise ProviderHTTPError("指定 Provider 暫時不可用或已達 Rate Limit", "VLM-005")
        self._local.channel = channel
        try:
            if boundary is None:
                response = getattr(channel.provider, method)(**kwargs)
            else:
                specification = channel.provider.process_spec()
                if specification is None:
                    boundary.record_cooperative()
                    response = getattr(channel.provider, method)(**kwargs)
                else:
                    response = boundary.call_provider(
                        specification,
                        method,
                        timeout_seconds=float(getattr(channel.provider, "timeout", 120)),
                        kwargs=kwargs,
                    )
        except Exception as exc:
            self.release_channel(channel, error=exc)
            raise
        self.release_channel(channel, usage=response.usage)
        return response

    def analyze(self, **kwargs) -> ProviderResponse:
        return self._execute("analyze", **kwargs)

    def analyze_isolated(self, boundary, **kwargs) -> ProviderResponse:
        """Keep routing/rate-limit state in the parent; isolate only SDK HTTP."""

        last_error: Exception | None = None
        for channel in self.channels:
            if not self.acquire_channel(channel):
                continue
            self._local.channel = channel
            try:
                specification = channel.provider.process_spec()
                if specification is None:
                    boundary.record_cooperative()
                    response = channel.provider.analyze(**kwargs)
                else:
                    response = boundary.call_provider(
                        specification,
                        "analyze",
                        timeout_seconds=float(getattr(channel.provider, "timeout", 120)),
                        kwargs=kwargs,
                    )
            except Exception as exc:
                last_error = exc
                self.release_channel(channel, error=exc)
                if bool(getattr(exc, "vision_started", False)) or bool(
                    getattr(exc, "request_started", False)
                ) or bool(getattr(exc, "ambiguous", False)):
                    raise
                continue
            self.release_channel(channel, usage=response.usage)
            return response
        if last_error:
            raise last_error
        raise ProviderHTTPError("所有 Provider 暫時不可用或已達 Rate Limit", "VLM-005")

    def repair_json(self, **kwargs) -> ProviderResponse:
        channel = getattr(self._local, "channel", None)
        if channel is None:
            return self._execute("repair_json", **kwargs)
        return self._execute_sticky(channel, "repair_json", **kwargs)

    def repair_json_isolated(self, boundary, **kwargs) -> ProviderResponse:
        channel = getattr(self._local, "channel", None)
        if channel is None:
            return self._execute("repair_json", **kwargs)
        return self._execute_sticky(channel, "repair_json", boundary=boundary, **kwargs)

    def submit_batch(self, batch_requests, completion_window="24h") -> str:
        last_error: Exception | None = None
        for channel in self.channels:
            try:
                result = channel.provider.submit_batch(
                    batch_requests, completion_window=completion_window
                )
                self._local.channel = channel
                return result
            except Exception as exc:
                last_error = exc
                if should_log_rate_limited(
                    f"batch-provider-failover:{channel.provider.name}", interval_seconds=5
                ):
                    log_event(
                        LOGGER,
                        logging.WARNING,
                        "Batch provider submission failed; failover will continue",
                        event="batch_provider_failover",
                        error_code=str(getattr(exc, "code", "VLM-007")),
                        provider=channel.provider.name,
                        provider_id=str(
                            getattr(channel.provider, "provider_id", channel.provider.name)
                        ),
                        operation="batch_submit",
                        failure_class=type(exc).__name__,
                        retryable=True,
                        ambiguous=bool(getattr(exc, "ambiguous", False)),
                    )
                continue
        error = ProviderHTTPError("所有 Provider 的 Batch 提交均失敗", "VLM-007")
        raise error from last_error

    def _batch_provider(self):
        channel = getattr(self._local, "channel", None)
        return channel.provider if channel is not None else self.channels[0].provider

    def upload_batch_file(self, path: Path, *, remote_filename: str | None = None) -> str:
        provider = self._batch_provider()
        result = provider.upload_batch_file(path, remote_filename=remote_filename)
        return str(result)

    def build_analysis_request_body(self, **kwargs) -> dict:
        return self._batch_provider().build_analysis_request_body(**kwargs)

    def create_batch(
        self,
        input_file_id: str,
        *,
        completion_window: str = "24h",
        metadata: dict | None = None,
        output_expires_after_seconds: int | None = None,
    ) -> dict:
        return self._batch_provider().create_batch(
            input_file_id,
            completion_window=completion_window,
            metadata=metadata,
            output_expires_after_seconds=output_expires_after_seconds,
        )

    def poll_batch(self, batch_id: str) -> dict:
        return self._batch_provider().retrieve_batch(batch_id)

    def retrieve_batch(self, batch_id: str) -> dict:
        return self._batch_provider().retrieve_batch(batch_id)

    def retrieve_file(self, file_id: str) -> dict:
        return self._batch_provider().retrieve_file(file_id)

    def cancel_batch(self, batch_id: str) -> dict:
        return self._batch_provider().cancel_batch(batch_id)

    def download_file_content(self, file_id: str, destination: Path) -> Path:
        return self._batch_provider().download_file_content(file_id, destination)

    def delete_remote_file(self, file_id: str) -> dict:
        return self._batch_provider().delete_remote_file(file_id)

    def estimate_cost(self, model: str, usage: Usage) -> float | None:
        channel = getattr(self._local, "channel", self.channels[0])
        return channel.provider.estimate_cost(model, usage)

    def estimate_batch_cost(self, model: str, usage: Usage) -> float | None:
        channel = getattr(self._local, "channel", self.channels[0])
        return channel.provider.estimate_batch_cost(model, usage)

    def validate_config(self) -> tuple[bool, str]:
        results = [channel.provider.validate_config() for channel in self.channels]
        return (any(result[0] for result in results), "；".join(result[1] for result in results))

    def close(self) -> None:
        """Release all HTTP sessions deterministically after a worker job."""
        for channel in self.channels:
            close = getattr(channel.provider, "close", None)
            if callable(close):
                close()
