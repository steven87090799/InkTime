from __future__ import annotations

import socket

import pytest

from inktime.app.services.stock_transport import (
    StockLanTransport,
    StockTransportError,
    UnsafeStockEndpoint,
    resolve_stock_target,
    validate_stock_endpoint_host,
)


def _resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.50", 80))]


@pytest.mark.parametrize("value", ["https://192.168.1.50", "192.168.1.50:80", "192.168.1.50/path", "user@host"])
def test_stock_endpoint_accepts_only_bare_hosts(value):
    with pytest.raises(UnsafeStockEndpoint):
        validate_stock_endpoint_host(value)


def test_stock_dns_rejects_public_or_mixed_results():
    def mixed(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.50", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        ]

    with pytest.raises(UnsafeStockEndpoint):
        resolve_stock_target("display.local", resolver=mixed)


class _FakeSocket:
    def settimeout(self, _value):
        return None


class _FakeResponse:
    status = 202

    def getheaders(self):
        return [("Content-Type", "text/plain")]

    def read(self, _limit):
        return b"accepted"


class _FakeConnection:
    def __init__(self):
        self.sock = _FakeSocket()
        self.calls = []
        self.closed = False

    def connect(self):
        return None

    def request(self, method, target, *, body, headers):
        self.calls.append((method, target, body, headers))

    def getresponse(self):
        return _FakeResponse()

    def close(self):
        self.closed = True


def test_stock_upload_is_one_shot_and_reports_acceptance_without_completion():
    connection = _FakeConnection()
    transport = StockLanTransport(
        resolver=_resolver,
        connection_factory=lambda _target, _address, _timeout: connection,
    )

    result = transport.upload("display.local", b"payload")

    assert result.status_code == 202
    assert result.body == b"accepted"
    assert result.headers["content-type"] == "text/plain"
    assert connection.calls[0][0:2] == ("POST", "/dataUP")
    assert connection.calls[0][2] == b"payload"
    assert connection.closed is True


def test_stock_upload_rejects_redirect_without_following():
    class Redirect(_FakeResponse):
        status = 302

    class RedirectConnection(_FakeConnection):
        def getresponse(self):
            return Redirect()

    connection = RedirectConnection()
    transport = StockLanTransport(
        resolver=_resolver,
        connection_factory=lambda _target, _address, _timeout: connection,
    )

    with pytest.raises(StockTransportError) as error:
        transport.upload("display.local", b"payload")
    assert error.value.code == "redirect_rejected"
    assert connection.closed is True
