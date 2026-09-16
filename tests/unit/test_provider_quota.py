from pathlib import Path
import multiprocessing

from inktime.app.db import Database, migrate
from inktime.app.providers.base import Usage
from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel
from inktime.app.repositories.provider_quota import ProviderQuotaRepository
from tests.unit.test_provider_router import StubProvider


def _router(path, *, scope="same-account", rpm=100, tpm=100, concurrency=1):
    return FailoverVisionProvider([ProviderChannel(
        StubProvider("provider"), requests_per_minute=rpm, tokens_per_minute=tpm,
        max_concurrency=concurrency, request_token_reserve=60,
        shared_quota=ProviderQuotaRepository(Database(Path(path)), scope),
    )], failure_threshold=1)


def _try_from_child(path, queue):
    router = _router(path)
    queue.put(router.acquire_channel(router.channels[0]))


def test_inflight_quota_is_shared_across_processes(tmp_path):
    path = tmp_path / "quota.sqlite3"
    migrate(Database(path))
    first = _router(path)
    assert first.acquire_channel(first.channels[0])
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    child = context.Process(target=_try_from_child, args=(str(path), queue))
    child.start()
    try:
        assert queue.get(timeout=10) is False
        child.join(timeout=10)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
        queue.close()
        first.release_channel(first.channels[0], usage=Usage(input_tokens=1))
    restarted = _router(path)
    assert restarted.acquire_channel(restarted.channels[0])
    restarted.release_channel(restarted.channels[0], usage=Usage(input_tokens=1))


def test_rpm_and_circuit_survive_router_reconstruction(tmp_path):
    path = tmp_path / "quota.sqlite3"
    migrate(Database(path))
    router = _router(path, rpm=1)
    assert router.acquire_channel(router.channels[0])
    router.release_channel(router.channels[0], usage=Usage(input_tokens=1))
    another = _router(path, rpm=1)
    assert not another.acquire_channel(another.channels[0])
    other_account = _router(path, scope="other-account")
    assert other_account.acquire_channel(other_account.channels[0])
    other_account.release_channel(other_account.channels[0], error=ProviderHTTPError("failure", "VLM-005"))
    restarted = _router(path, scope="other-account")
    assert not restarted.acquire_channel(restarted.channels[0])
