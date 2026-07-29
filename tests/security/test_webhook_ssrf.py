from __future__ import annotations

import socket
import ssl

import pytest

from inktime.app.core.webhook_safety import (
    PinnedWebhookTransport,
    UnsafeWebhookURL,
    resolve_webhook_destination,
)


def _records(*addresses: str):
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (address, 443),
        )
        for address in addresses
    ]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "::1",
        "::ffff:192.168.1.10",
        "0.0.0.0",  # noqa: S104 - blocked destination fixture.
        "224.0.0.1",
    ],
)
def test_webhook_rejects_non_global_ipv4_ipv6_and_mapped_addresses(address):
    with pytest.raises(UnsafeWebhookURL, match="WEBHOOK-SSRF-003"):
        resolve_webhook_destination(
            "https://hooks.example.net/inktime",
            resolver=lambda *_args, **_kwargs: _records(address),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.net/inktime",
        "https://user:secret@hooks.example.net/inktime",
        "file:///etc/passwd",
        "https://hooks.example.net/inktime#secret",
        "https://127.1/inktime",
    ],
)
def test_webhook_rejects_unsafe_url_forms(url):
    with pytest.raises(UnsafeWebhookURL):
        resolve_webhook_destination(
            url,
            resolver=lambda *_args, **_kwargs: _records("8.8.8.8"),
        )


def test_webhook_allowlist_exact_suffix_ip_and_cidr_are_boundary_aware():
    def private_resolver(*_args, **_kwargs):
        return _records("10.20.30.40")

    assert resolve_webhook_destination(
        "https://a.example.com/hook",
        resolver=private_resolver,
        allowlist=(".example.com",),
    ).addresses == ("10.20.30.40",)
    assert resolve_webhook_destination(
        "https://exact.example.net/hook",
        resolver=private_resolver,
        allowlist=("exact.example.net",),
    ).addresses == ("10.20.30.40",)
    assert resolve_webhook_destination(
        "https://private.example.net/hook",
        resolver=private_resolver,
        allowlist=("10.20.30.40",),
    ).addresses == ("10.20.30.40",)
    assert resolve_webhook_destination(
        "https://private.example.net/hook",
        resolver=private_resolver,
        allowlist=("10.20.0.0/16",),
    ).addresses == ("10.20.30.40",)
    with pytest.raises(UnsafeWebhookURL):
        resolve_webhook_destination(
            "https://evil-example.com/hook",
            resolver=private_resolver,
            allowlist=(".example.com",),
        )


class _Response:
    status = 302

    def read(self, _limit):
        return b""

    def getheaders(self):
        return [("Location", "http://127.0.0.1/private")]


class _Connection:
    def __init__(self, calls):
        self.calls = calls

    def request(self, method, target, *, body, headers):
        self.calls.append(
            {
                "method": method,
                "target": target,
                "body": body,
                "headers": headers,
            }
        )

    def getresponse(self):
        return _Response()

    def close(self):
        return None


def test_webhook_transport_pins_validated_ip_preserves_tls_hostname_and_never_redirects():
    resolver_calls = []
    connection_calls = []
    requests = []

    def resolver(host, port, **_kwargs):
        resolver_calls.append((host, port))
        return _records("8.8.8.8")

    def factory(target, address, timeout, context):
        connection_calls.append((target, address, timeout, context))
        return _Connection(requests)

    transport = PinnedWebhookTransport(
        resolver=resolver,
        connection_factory=factory,
    )
    response = transport.post(
        "https://hooks.example.net:8443/inktime?event=device",
        json={"status": "ok"},
        headers={"Authorization": "Bearer secret"},
        timeout=(2.0, 5.0),
        allow_redirects=False,
    )

    assert response.status_code == 302
    assert resolver_calls == [("hooks.example.net", 8443)]
    target, address, timeout, context = connection_calls[0]
    assert address == "8.8.8.8"
    assert target.hostname == "hooks.example.net"
    assert timeout == 2.0
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert requests[0]["headers"]["Host"] == "hooks.example.net:8443"
    assert len(requests) == 1


def test_webhook_token_is_not_sent_when_destination_is_rejected():
    connections = []

    def forbidden_factory(*args):
        connections.append(args)
        raise AssertionError("private destination must not reach the connection layer")

    transport = PinnedWebhookTransport(
        resolver=lambda *_args, **_kwargs: _records("127.0.0.1"),
        connection_factory=forbidden_factory,
    )
    with pytest.raises(UnsafeWebhookURL):
        transport.post(
            "https://private.example.net/hook",
            json={"event": "test"},
            headers={"Authorization": "Bearer must-not-leak"},
            timeout=(2.0, 5.0),
            allow_redirects=False,
        )
    assert connections == []
