from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
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
    cooldown_seconds: int = 300
    semaphore: threading.BoundedSemaphore = field(init=False)
    request_times: deque = field(default_factory=deque)
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

    def _available(self, channel: ProviderChannel) -> bool:
        now = time.monotonic()
        with self._lock:
            while channel.request_times and channel.request_times[0] <= now - 60:
                channel.request_times.popleft()
            if channel.circuit_until > now:
                return False
            if channel.requests_per_minute and len(channel.request_times) >= channel.requests_per_minute:
                return False
            channel.request_times.append(now)
            return True

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
                        channel.circuit_until = time.monotonic() + max(float(retry_after or 0), channel.cooldown_seconds)
                continue
            finally:
                channel.semaphore.release()
            with self._lock:
                channel.failures = 0
            self._local.channel = channel
            return response
        if last_error:
            raise last_error
        raise ProviderHTTPError("所有 Provider 暫時不可用或已達 Rate Limit", "VLM-005")

    def analyze(self, **kwargs) -> ProviderResponse:
        return self._execute("analyze", **kwargs)

    def repair_json(self, **kwargs) -> ProviderResponse:
        channel = getattr(self._local, "channel", None)
        if channel is None:
            return self._execute("repair_json", **kwargs)
        return channel.provider.repair_json(**kwargs)

    def submit_batch(self, requests, completion_window="24h") -> str:
        for channel in self.channels:
            try:
                result = channel.provider.submit_batch(requests, completion_window=completion_window)
                self._local.channel = channel
                return result
            except Exception:
                continue
        raise ProviderHTTPError("所有 Provider 的 Batch 提交均失敗", "VLM-007")

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
