from __future__ import annotations

from pathlib import Path
import time

import pytest

from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.providers.openai_compatible import ProviderHTTPError
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


class CooperativeBoundary:
    def __init__(self):
        self.cooperative_calls = 0

    def record_cooperative(self):
        self.cooperative_calls += 1


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


def test_candidate_inspection_does_not_consume_rpm_or_network_permit():
    provider = StubProvider("cache-owner")
    channel = ProviderChannel(provider, requests_per_minute=1, max_concurrency=1)
    router = FailoverVisionProvider([channel])

    assert router.candidate_channels() == [channel]
    assert router.candidate_channels() == [channel]
    assert not channel.request_times
    assert router.acquire_channel(channel) is True
    assert len(channel.request_times) == 1
    router.release_channel(channel, usage=Usage(input_tokens=10, output_tokens=2))
    assert router.candidate_channels() == []


def test_route_channels_keep_cache_identities_when_network_is_unavailable():
    provider = StubProvider("cache-owner")
    channel = ProviderChannel(provider, requests_per_minute=1, tokens_per_minute=10)
    router = FailoverVisionProvider([channel])
    now = time.monotonic()
    channel.circuit_until = now + 60
    channel.request_times.append(now)
    channel.token_events.append((now, 10))

    assert router.route_channels() == [channel]
    assert list(channel.request_times) == [now]
    assert list(channel.token_events) == [(now, 10)]
    assert channel.semaphore.acquire(blocking=False) is True
    channel.semaphore.release()


def test_failed_cache_owner_releases_permit_and_opens_circuit():
    provider = StubProvider("broken")
    channel = ProviderChannel(provider, max_concurrency=1, cooldown_seconds=60)
    router = FailoverVisionProvider([channel], failure_threshold=1)

    assert router.acquire_channel(channel) is True
    router.release_channel(channel, error=ProviderHTTPError("retry", "VLM-005", retry_after=1))
    assert router.candidate_channels() == []
    with pytest.raises(ProviderHTTPError):
        router.select_channel()
    assert channel.semaphore.acquire(blocking=False) is True
    channel.semaphore.release()


def test_isolated_router_keeps_failover_and_repair_on_the_selected_provider():
    broken = StubProvider("broken", fails=True)
    healthy = StubProvider("healthy", tokens=12)
    router = FailoverVisionProvider(
        [ProviderChannel(broken, priority=1), ProviderChannel(healthy, priority=2)],
        failure_threshold=1,
    )
    boundary = CooperativeBoundary()

    response = router.analyze_isolated(
        boundary, image_path=Path("x"), model="m", detail="low", stage="one"
    )
    repaired = router.repair_json_isolated(
        boundary, invalid_content="{}", validation_error="invalid", model="m"
    )
    repaired_directly = router.repair_json(
        invalid_content="{}", validation_error="invalid", model="m"
    )
    assert router.submit_batch([{"request": "x"}]) == "batch"
    assert router.poll_batch("batch") == {}
    assert router.cancel_batch("batch") == {}
    assert router.estimate_cost("m", Usage()) == 0
    assert router.validate_config() == (True, "ok；ok")

    assert response.usage.input_tokens == 12
    assert repaired.usage.input_tokens == 12
    assert repaired_directly.usage.input_tokens == 12
    assert broken.calls == 1
    assert healthy.calls == 3
    assert boundary.cooperative_calls == 3


def test_provider_service_uses_only_frozen_route_and_rejects_changed_members(app):
    repository = app.extensions["inktime_provider_repository"]
    service = app.extensions["inktime_provider_service"]

    def save(name: str, priority: int) -> str:
        return repository.save(
            {
                "name": name,
                "base_url": "https://example.invalid/v1",
                "api_key": f"secret-{name}",
                "priority": priority,
                "enabled": True,
            },
            user_id="test",
        )

    first = save("first", 20)
    second = save("second", 10)
    snapshot = service.route_snapshot()
    assert [item["provider_id"] for item in snapshot] == [second, first]
    assert all("secret" not in str(item).casefold() for item in snapshot)
    late = save("late", 1)

    router = service.build_router(snapshot, scoring_rules="frozen rules")
    assert [channel.provider.provider_id for channel in router.channels] == [second, first]
    assert all(channel.provider.provider_id != late for channel in router.channels)
    assert router.channels[0].provider.scoring_rules == "frozen rules"

    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE providers SET enabled=0 WHERE id=?", (second,))
    with pytest.raises(ValueError, match="已停用"):
        service.build_router(snapshot)

    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE providers SET enabled=1 WHERE id=?", (second,))
        connection.execute("UPDATE providers SET base_url='https://changed.invalid/v1' WHERE id=?", (first,))
    with pytest.raises(ValueError, match="設定已變更"):
        service.build_router(snapshot)


def test_empty_frozen_route_never_discovers_a_later_provider(app):
    repository = app.extensions["inktime_provider_repository"]
    service = app.extensions["inktime_provider_service"]

    assert service.build_router([]) is None
    repository.save(
        {
            "name": "late",
            "base_url": "https://example.invalid/v1",
            "api_key": "late-secret",
            "enabled": True,
        },
        user_id="test",
    )
    assert service.build_router([]) is None
    assert service.build_router() is not None


def test_provider_config_revision_ignores_secret_rotation_and_rejects_behavior_changes(app):
    repository = app.extensions["inktime_provider_repository"]
    service = app.extensions["inktime_provider_service"]
    provider_id = repository.save(
        {
            "name": "stable",
            "base_url": "https://example.invalid/v1",
            "api_key": "first-secret",
            "timeout_seconds": 30,
            "enabled": True,
        },
        user_id="test",
    )
    snapshot = service.route_snapshot()
    assert len(snapshot) == 1
    assert "secret" not in str(snapshot).casefold()
    original_revision = snapshot[0]["config_revision"]

    repository.save(
        {
            "id": provider_id,
            "name": "stable",
            "base_url": "https://example.invalid/v1",
            "api_key": "rotated-secret",
            "timeout_seconds": 30,
            "enabled": True,
        },
        user_id="test",
    )
    assert service.route_snapshot()[0]["config_revision"] == original_revision
    assert service.build_router(snapshot) is not None

    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE providers SET timeout_seconds=31 WHERE id=?", (provider_id,))
    with pytest.raises(ValueError, match="設定已變更"):
        service.build_router(snapshot)

    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE providers SET timeout_seconds=30, enabled=0 WHERE id=?", (provider_id,))
    with pytest.raises(ValueError, match="已停用"):
        service.build_router(snapshot)

    with app.extensions["inktime_database"].session() as connection:
        connection.execute("DELETE FROM providers WHERE id=?", (provider_id,))
    with pytest.raises(ValueError, match="已刪除"):
        service.build_router(snapshot)
