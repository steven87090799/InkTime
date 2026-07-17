from __future__ import annotations

from pathlib import Path

from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel


class StubProvider(VisionProvider):
    def __init__(self, name, fails=False, tokens=0):
        self.name = name
        self.fails = fails
        self.calls = 0
        self.tokens = tokens

    def analyze(self, **kwargs):
        self.calls += 1
        if self.fails:
            raise RuntimeError("故障")
        return ProviderResponse("{}", Usage(input_tokens=self.tokens))

    def repair_json(self, **kwargs):
        return self.analyze(**kwargs)

    def submit_batch(self, requests, completion_window="24h"):
        return "batch"

    def poll_batch(self, batch_id):
        return {}

    def cancel_batch(self, batch_id):
        return {}

    def estimate_cost(self, model, usage):
        return 0

    def validate_config(self):
        return True, "ok"


def test_provider_failure_falls_over_and_circuit_opens():
    broken = StubProvider("broken", fails=True)
    healthy = StubProvider("healthy")
    router = FailoverVisionProvider(
        [
            ProviderChannel(broken, priority=1, cooldown_seconds=60),
            ProviderChannel(healthy, priority=2),
        ],
        failure_threshold=1,
    )
    router.analyze(image_path=Path("x"), model="m", detail="low", stage="one")
    router.analyze(image_path=Path("x"), model="m", detail="low", stage="one")
    assert broken.calls == 1
    assert healthy.calls == 2
    assert router.name == "healthy"


def test_rpm_limit_skips_busy_provider():
    first = StubProvider("limited")
    second = StubProvider("fallback")
    router = FailoverVisionProvider(
        [
            ProviderChannel(first, priority=1, requests_per_minute=1),
            ProviderChannel(second, priority=2),
        ]
    )
    router.analyze(image_path=Path("x"), model="m", detail="low", stage="one")
    router.analyze(image_path=Path("x"), model="m", detail="low", stage="one")
    assert first.calls == 1
    assert second.calls == 1


def test_tpm_limit_skips_provider_after_recorded_usage():
    first = StubProvider("token-limited", tokens=100)
    second = StubProvider("fallback")
    router = FailoverVisionProvider(
        [
            ProviderChannel(first, priority=1, tokens_per_minute=100),
            ProviderChannel(second, priority=2),
        ]
    )
    router.analyze(image_path=Path("x"), model="m", detail="low", stage="one")
    router.analyze(image_path=Path("x"), model="m", detail="low", stage="one")
    assert first.calls == 1
    assert second.calls == 1
