from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time

from .base import ProviderResponse, Usage, VisionProvider
from .openai_compatible import ProviderHTTPError


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
        if self._available(channel):
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
            if self._available(channel) and channel.semaphore.acquire(blocking=False):
                self._local.channel = channel
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
        for channel in self.channels:
            if not self._available(channel) or not channel.semaphore.acquire(blocking=False):
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
                continue
            finally:
                channel.semaphore.release()
            with self._lock:
                channel.failures = 0
                used_tokens = response.usage.input_tokens + response.usage.output_tokens
                if used_tokens:
                    channel.token_events.append((time.monotonic(), used_tokens))
            self._local.channel = channel
            return response
        if last_error:
            raise last_error
        raise ProviderHTTPError("所有 Provider 暫時不可用或已達 Rate Limit", "VLM-005")

    def analyze(self, **kwargs) -> ProviderResponse:
        return self._execute("analyze", **kwargs)

    def analyze_isolated(self, boundary, **kwargs) -> ProviderResponse:
        """Keep routing/rate-limit state in the parent; isolate only SDK HTTP."""

        last_error: Exception | None = None
        for channel in self.channels:
            if not self._available(channel) or not channel.semaphore.acquire(blocking=False):
                continue
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
                with self._lock:
                    channel.failures += 1
                    if channel.failures >= self.failure_threshold:
                        channel.circuit_until = time.monotonic() + channel.cooldown_seconds
                continue
            finally:
                channel.semaphore.release()
            with self._lock:
                channel.failures = 0
                used_tokens = response.usage.input_tokens + response.usage.output_tokens
                if used_tokens:
                    channel.token_events.append((time.monotonic(), used_tokens))
            self._local.channel = channel
            return response
        if last_error:
            raise last_error
        raise ProviderHTTPError("所有 Provider 暫時不可用或已達 Rate Limit", "VLM-005")

    def repair_json(self, **kwargs) -> ProviderResponse:
        channel = getattr(self._local, "channel", None)
        if channel is None:
            return self._execute("repair_json", **kwargs)
        return channel.provider.repair_json(**kwargs)

    def repair_json_isolated(self, boundary, **kwargs) -> ProviderResponse:
        channel = getattr(self._local, "channel", None)
        if channel is None:
            return self._execute("repair_json", **kwargs)
        specification = channel.provider.process_spec()
        if specification is None:
            boundary.record_cooperative()
            return channel.provider.repair_json(**kwargs)
        return boundary.call_provider(
            specification,
            "repair_json",
            timeout_seconds=float(getattr(channel.provider, "timeout", 120)),
            kwargs=kwargs,
        )

    def submit_batch(self, requests, completion_window="24h") -> str:
        last_error: Exception | None = None
        for channel in self.channels:
            try:
                result = channel.provider.submit_batch(requests, completion_window=completion_window)
                self._local.channel = channel
                return result
            except Exception as exc:
                last_error = exc
                continue
        error = ProviderHTTPError("所有 Provider 的 Batch 提交均失敗", "VLM-007")
        raise error from last_error

    def _batch_provider(self):
        channel = getattr(self._local, "channel", None)
        return channel.provider if channel is not None else self.channels[0].provider

    def upload_batch_file(self, path: Path) -> str:
        provider = self._batch_provider()
        result = provider.upload_batch_file(path)
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

    def cancel_batch(self, batch_id: str) -> dict:
        return self._batch_provider().cancel_batch(batch_id)

    def download_file_content(self, file_id: str, destination: Path) -> Path:
        return self._batch_provider().download_file_content(file_id, destination)

    def delete_remote_file(self, file_id: str) -> dict:
        return self._batch_provider().delete_remote_file(file_id)

    def estimate_cost(self, model: str, usage: Usage) -> float:
        channel = getattr(self._local, "channel", self.channels[0])
        return channel.provider.estimate_cost(model, usage)

    def estimate_batch_cost(self, model: str, usage: Usage) -> float:
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
