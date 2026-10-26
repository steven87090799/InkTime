"""Free-first routing uses official account quota; all transports are mocks."""

import time

import pytest

from inktime.app.providers.base import request_definitely_not_sent
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError
from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel
from tests.unit.test_provider_router import StubProvider


def router_with_quota(remaining):
    quota = {"used": 1000 - remaining, "limit": 1000, "remaining": remaining}
    free, paid = StubProvider("free"), StubProvider("paid")
    for provider in (free, paid):
        provider.base_url = "https://openrouter.ai/api/v1"
        provider.api_key = "mock-account-key"
    free.free_daily_quota = lambda: dict(quota)
    channels = [
        ProviderChannel(free, model="openrouter/free", priority=1, daily_free_first=True),
        ProviderChannel(paid, model="nvidia/nemotron-3.5-super-vl", priority=2, daily_free_first=True),
    ]
    return FailoverVisionProvider(channels), free, paid, quota


def test_free_is_used_until_official_quota_is_zero_then_paid_and_back_to_free():
    router, free, paid, quota = router_with_quota(1)
    router.analyze()
    assert (free.calls, paid.calls) == (1, 0)
    quota.update(used=1000, remaining=0)
    router.analyze()
    assert (free.calls, paid.calls) == (1, 1)
    assert paid.last_kwargs["model"] == "nvidia/nemotron-3.5-super-vl"
    quota.update(used=0, remaining=1000)
    router.analyze()
    assert (free.calls, paid.calls) == (2, 1)


def test_cooldown_does_not_enable_paid_while_free_requests_remain():
    router, free, paid, _ = router_with_quota(999)
    router.channels[0].circuit_until = time.monotonic() + 300
    with pytest.raises(ProviderHTTPError) as raised:
        router.analyze()
    assert request_definitely_not_sent(raised.value)
    assert raised.value.retry_after > 290
    assert free.calls == paid.calls == 0
    assert not router.channels[1].request_times
    assert router.channels[1].semaphore.acquire(blocking=False)


def test_free_only_exhaustion_preserves_queue_until_utc_reset():
    router, free, paid, _ = router_with_quota(0)
    router.channels = router.channels[:1]
    with pytest.raises(ProviderHTTPError) as raised:
        router.analyze()
    assert request_definitely_not_sent(raised.value)
    assert 0 < raised.value.retry_after <= 86400
    assert free.calls == paid.calls == 0
    assert not router.channels[0].request_times


def test_missing_quota_evidence_never_dispatches_paid():
    router, free, paid, _ = router_with_quota(0)

    def unavailable():
        raise ProviderHTTPError("quota lookup unavailable", "VLM-005", retry_after=60)

    free.free_daily_quota = unavailable
    with pytest.raises(ProviderHTTPError):
        router.analyze()
    assert free.calls == paid.calls == 0


def test_quota_gating_does_not_reset_a_provider_failure_streak():
    router, _, _, _ = router_with_quota(0)
    channel = router.channels[0]
    channel.failures = 2
    assert not router.acquire_channel(channel)
    assert channel.failures == 2
    assert not channel.request_times


@pytest.mark.parametrize("quota", [
    {}, {"used": 1000, "limit": 1000, "remaining": True},
    {"used": 999, "limit": 1000, "remaining": 0},
    {"used": 0, "limit": 0, "remaining": 0},
])
def test_quota_endpoint_requires_explicit_consistent_account_evidence(quota):
    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"data": {"free_model_daily_requests": quota}}

    class Session:
        def get(self, url, **kwargs):
            assert url == "https://openrouter.ai/api/v1/key"
            assert kwargs["allow_redirects"] is False
            return Response()

    provider = OpenAICompatibleProvider(
        name="mock", kind="openrouter", base_url="https://openrouter.ai/api/v1",
        api_key="mock-key", session=Session(),
    )
    with pytest.raises(ProviderHTTPError) as raised:
        provider.free_daily_quota()
    assert request_definitely_not_sent(raised.value)


def test_paid_free_first_requires_matching_free_route_and_positive_budget(app):
    repository = app.extensions["inktime_provider_repository"]
    service = app.extensions["inktime_provider_service"]
    repository.save({
        "name": "paid", "kind": "openrouter", "base_url": "https://openrouter.ai/api/v1",
        "api_key": "mock-key", "model": "nvidia/nemotron-3.5-super-vl",
        "options": {"daily_free_first": True}, "enabled": True, "priority": 200,
    }, user_id="test")
    with pytest.raises(ValueError, match="CONFIG_INVALID"):
        service.build_router(service.route_snapshot())


@pytest.mark.parametrize("cap,valid", [("0", False), ("5", True)])
def test_paid_route_requires_budget_and_uses_same_account_serial_permit(app, cap, valid):
    repository = app.extensions["inktime_provider_repository"]
    service = app.extensions["inktime_provider_service"]
    app.extensions["inktime_settings_repository"].update(
        "budget.daily_stop", cap, changed_by="test", source_ip="127.0.0.1",
    )
    for name, model, priority in [("free", "openrouter/free", 10), ("paid", "nvidia/nemotron-3.5-super-vl", 20)]:
        repository.save({
            "name": name, "kind": "openrouter", "base_url": "https://openrouter.ai/api/v1",
            "api_key": "mock-key", "model": model, "options": {"daily_free_first": True},
            "enabled": True, "priority": priority,
        }, user_id="test")
    if not valid:
        with pytest.raises(ValueError, match="CONFIG_INVALID"):
            service.build_router(service.route_snapshot())
        return
    router = service.build_router(service.route_snapshot())
    assert router.channels[0].shared_quota.scope == router.channels[1].shared_quota.scope
    assert all(channel.max_concurrency == 1 for channel in router.channels)
    assert router.channels[0].requests_per_minute == 20


def test_daily_free_first_option_is_boolean_and_openrouter_only():
    from inktime.app.providers.config import normalize_options

    assert normalize_options("openrouter", {"daily_free_first": True})["daily_free_first"] is True
    with pytest.raises(ValueError):
        normalize_options("openrouter", {"daily_free_first": "true"})
    with pytest.raises(ValueError):
        normalize_options("openai", {"daily_free_first": True})


def test_quota_read_is_authenticated_and_does_not_send_an_image():
    class Response:
        status_code = 200

        def json(self):
            return {"data": {"free_model_daily_requests": {"used": 7, "limit": 1000, "remaining": 993}}}

    class Session:
        def get(self, url, **kwargs):
            assert url == "https://openrouter.ai/api/v1/key"
            assert kwargs["headers"]["Authorization"] == "Bearer mock-key"
            assert "json" not in kwargs
            return Response()

    provider = OpenAICompatibleProvider(
        name="mock", kind="openrouter", base_url="https://openrouter.ai/api/v1",
        api_key="mock-key", session=Session(),
    )
    assert provider.free_daily_quota() == {"used": 7, "limit": 1000, "remaining": 993}
