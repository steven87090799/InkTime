from __future__ import annotations

import pytest

from inktime.app.providers.config import validate_base_url
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider


@pytest.mark.parametrize(
    "base_url",
    [
        "http://192.168.1.10:11434",
        "http://10.0.0.8:11434",
        "http://172.16.0.8:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
    ],
)
def test_private_http_requires_explicit_opt_in_and_accepts_literal_ip(base_url):
    with pytest.raises(ValueError, match="PROVIDER-019"):
        validate_base_url("openai_compatible", base_url)
    assert validate_base_url("openai_compatible", base_url, {"allow_private_http": True}) == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://8.8.8.8:11434",
        "http://provider.internal:11434",
        "http://provider.local:11434",
        "http://provider.lan:11434",
        "http://localhost:11434",
    ],
)
def test_private_http_rejects_public_and_unpinned_hostnames(base_url):
    with pytest.raises(ValueError, match="PROVIDER-018"):
        validate_base_url("openai_compatible", base_url, {"allow_private_http": True})


def test_https_hostname_and_openrouter_are_unaffected():
    assert validate_base_url("openai_compatible", "https://provider.internal/v1") == "https://provider.internal/v1"
    assert validate_base_url("openrouter", "https://openrouter.ai/api/v1") == "https://openrouter.ai/api/v1"


def test_provider_constructor_does_not_bypass_private_http_opt_in():
    with pytest.raises(ValueError, match="PROVIDER-019"):
        OpenAICompatibleProvider(
            name="local",
            base_url="http://127.0.0.1:11434/v1",
            api_key="test-key",
            kind="ollama",
        )


def test_provider_requests_disable_redirects_and_keep_authorization_bound():
    class Response:
        status_code = 200

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

        def close(self):
            return None

    session = Session()
    provider = OpenAICompatibleProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        kind="openrouter",
        session=session,
    )
    provider._send(
        "POST",
        "/chat/completions",
        headers={"Authorization": "Bearer test-key"},
        json={},
    )
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer test-key"
    provider.close()
