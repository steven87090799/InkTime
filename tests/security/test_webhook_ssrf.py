from __future__ import annotations

import socket
import ssl
import time

import pytest

from inktime.app.core.webhook_safety import (
    PinnedWebhookTransport,
    UnsafeWebhookURL,
    WebhookConnectTimeout,
    WebhookReadTimeout,
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

    def __init__(self):
        self.closed = False

    def read(self, _limit):
        return b""

    def getheaders(self):
        return [("Location", "http://127.0.0.1/private")]

    def close(self):
        self.closed = True


class _Socket:
    def __init__(self):
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value


class _Connection:
    def __init__(self, calls):
        self.calls = calls
        self.sock = _Socket()
        self.response = _Response()
        self.closed = False

    def connect(self):
        return None

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
        return self.response

    def close(self):
        self.closed = True


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
    assert response.status_code == 302


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


def test_webhook_connect_timeout_is_applied_and_closes_connection():
    connection = _Connection([])

    def timeout_connect():
        raise TimeoutError("connect stalled")

    connection.connect = timeout_connect
    transport = PinnedWebhookTransport(
        resolver=lambda *_args, **_kwargs: _records("8.8.8.8"),
        connection_factory=lambda *_args: connection,
    )

    with pytest.raises(WebhookConnectTimeout) as raised:
        transport.post(
            "https://hooks.example.net/hook",
            json={"event": "test"},
            headers={},
            timeout=(0.05, 1.0),
            allow_redirects=False,
        )

    assert raised.value.code == "webhook_connect_timeout"
    assert connection.closed is True


def test_webhook_read_timeout_is_applied_after_connection_without_new_dns_lookup():
    resolver_calls = []
    connection = _Connection([])

    def resolver(*args, **_kwargs):
        resolver_calls.append(args)
        return _records("8.8.8.8")

    def delayed_response():
        assert connection.sock.timeout == 0.05
        time.sleep(connection.sock.timeout + 0.01)
        raise TimeoutError("response stalled")

    connection.getresponse = delayed_response
    transport = PinnedWebhookTransport(
        resolver=resolver,
        connection_factory=lambda *_args: connection,
    )

    with pytest.raises(WebhookReadTimeout) as raised:
        transport.post(
            "https://hooks.example.net/hook",
            json={"event": "test"},
            headers={},
            timeout=(2.0, 0.05),
            allow_redirects=False,
        )

    assert raised.value.code == "webhook_read_timeout"
    assert len(resolver_calls) == 1
    assert connection.closed is True


def test_webhook_fast_response_succeeds_with_separate_timeouts_and_closes_resources():
    connection = _Connection([])
    transport = PinnedWebhookTransport(
        resolver=lambda *_args, **_kwargs: _records("8.8.8.8"),
        connection_factory=lambda *_args: connection,
    )

    response = transport.post(
        "https://hooks.example.net/hook",
        json={"event": "test"},
        headers={},
        timeout=(1.5, 4.5),
        allow_redirects=False,
    )

    assert response.status_code == 302
    assert connection.sock.timeout == 4.5
    assert connection.response.closed is True
    assert connection.closed is True
