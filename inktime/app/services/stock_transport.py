"""SSRF-safe LAN transport for the Stock PhotoPainter ``/dataUP`` endpoint."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import math
import re
import socket
from typing import Any, Callable


class UnsafeStockEndpoint(ValueError):
    """The configured endpoint is not a bare, private-network host."""


class StockTransportError(OSError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StockTarget:
    hostname: str
    port: int
    request_target: str
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class StockUploadResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


Resolver = Callable[..., list[tuple[Any, ...]]]
ConnectionFactory = Callable[[StockTarget, str, float], http.client.HTTPConnection]
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def validate_stock_endpoint_host(value: str | None) -> str | None:
    """Validate and normalize a host with no URL syntax, port, or path."""

    if value is None or not str(value).strip():
        return None
    host = str(value).strip()
    if len(host) > 253 or any(character.isspace() for character in host):
        raise UnsafeStockEndpoint("DEVICE-009 Stock Host 必須是裸 LAN IP 或主機名稱")
    if any(marker in host for marker in ("://", "/", "?", "#", "@")):
        raise UnsafeStockEndpoint("DEVICE-009 Stock Host 不可包含 Scheme、Port、Path 或帳密")
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        address = None
    if address is not None:
        if isinstance(address, ipaddress.IPv6Address) and "%" in host:
            raise UnsafeStockEndpoint("DEVICE-009 Stock Host 不可包含 IPv6 Zone ID")
        return str(address)
    if ":" in host or host.startswith("[") or host.endswith("]"):
        raise UnsafeStockEndpoint("DEVICE-009 Stock Host 不可包含 Port")
    try:
        ascii_host = host.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise UnsafeStockEndpoint("DEVICE-009 Stock Host 名稱不合法") from exc
    if not ascii_host or len(ascii_host) > 253:
        raise UnsafeStockEndpoint("DEVICE-009 Stock Host 名稱不合法")
    labels = ascii_host.split(".")
    if any(len(label) > 63 or _HOST_LABEL.fullmatch(label) is None for label in labels):
        raise UnsafeStockEndpoint("DEVICE-009 Stock Host 名稱不合法")
    return ascii_host


def _normalized_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def resolve_stock_target(
    host: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> StockTarget:
    hostname = validate_stock_endpoint_host(host)
    if hostname is None:
        raise UnsafeStockEndpoint("DEVICE-009 尚未設定 Stock Host")
    try:
        records = resolver(hostname, 80, type=socket.SOCK_STREAM)
    except (OSError, TimeoutError) as exc:
        raise UnsafeStockEndpoint("DEVICE-009 Stock Host 無法解析") from exc
    addresses: list[str] = []
    for record in records:
        try:
            address = _normalized_ip(str(record[4][0]))
        except (IndexError, ValueError) as exc:
            raise UnsafeStockEndpoint("DEVICE-009 Stock DNS 回應不合法") from exc
        # Stock is a LAN-only protocol.  Reject every global or ambiguous
        # result, including a mixed private/public DNS response.
        if (
            address.is_global
            or address.is_loopback
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
            or not (address.is_private or address.is_link_local)
        ):
            raise UnsafeStockEndpoint("DEVICE-009 Stock Host 不可解析至公開或保留網路")
        canonical = str(address)
        if canonical not in addresses:
            addresses.append(canonical)
    if not addresses:
        raise UnsafeStockEndpoint("DEVICE-009 Stock Host 沒有可用 LAN 位址")
    return StockTarget(hostname, 80, "/dataUP", tuple(addresses))


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: StockTarget, address: str, *, timeout: float) -> None:
        super().__init__(target.hostname, target.port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._pinned_address, self.port), self.timeout)
        peer = _normalized_ip(str(raw_socket.getpeername()[0]))
        if peer != _normalized_ip(self._pinned_address):
            raw_socket.close()
            raise UnsafeStockEndpoint("DEVICE-009 實際 Stock Peer 與驗證位址不一致")
        self.sock = raw_socket


def _default_connection_factory(target: StockTarget, address: str, timeout: float) -> http.client.HTTPConnection:
    return _PinnedHTTPConnection(target, address, timeout=timeout)


class StockLanTransport:
    """One-shot, no-redirect, DNS-pinned HTTP upload transport."""

    def __init__(
        self,
        *,
        resolver: Resolver = socket.getaddrinfo,
        connection_factory: ConnectionFactory = _default_connection_factory,
        max_response_bytes: int = 64 * 1024,
    ) -> None:
        self.resolver = resolver
        self.connection_factory = connection_factory
        self.max_response_bytes = max(1024, min(int(max_response_bytes), 1024 * 1024))

    def upload(
        self,
        host: str,
        payload: bytes,
        *,
        connect_timeout: float = 3.0,
        read_timeout: float = 8.0,
    ) -> StockUploadResponse:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("DEVICE-009 Stock Payload 不可空白")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in (connect_timeout, read_timeout)
        ):
            raise ValueError("DEVICE-009 Stock timeout 必須是正數")
        target = resolve_stock_target(host, resolver=self.resolver)
        connection = self.connection_factory(target, target.addresses[0], float(connect_timeout))
        response: http.client.HTTPResponse | None = None
        try:
            try:
                connection.connect()
            except (TimeoutError, socket.timeout) as exc:
                raise StockTransportError("DEVICE-009 Stock LAN 連線逾時", code="connect_timeout") from exc
            if connection.sock is None:
                raise StockTransportError("DEVICE-009 Stock LAN 未建立連線", code="connect_failed")
            connection.sock.settimeout(float(read_timeout))
            connection.request(
                "POST",
                target.request_target,
                body=payload,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(payload)),
                    "Host": (
                        f"[{target.hostname}]" if ":" in target.hostname else target.hostname
                    ),
                    "Connection": "close",
                },
            )
            try:
                response = connection.getresponse()
                body = response.read(self.max_response_bytes + 1)
            except (TimeoutError, socket.timeout) as exc:
                raise StockTransportError("DEVICE-009 Stock 回應逾時；不自動重送", code="read_timeout") from exc
            if len(body) > self.max_response_bytes:
                raise StockTransportError("DEVICE-009 Stock 回應過大", code="response_too_large")
            status = int(response.status)
            if 300 <= status < 400:
                raise StockTransportError("DEVICE-009 Stock 不允許 Redirect", code="redirect_rejected")
            headers = {
                str(key).lower(): str(value)[:256]
                for key, value in response.getheaders()
            }
            return StockUploadResponse(status, headers, body)
        except StockTransportError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise StockTransportError("DEVICE-009 Stock LAN 上傳失敗；不自動重送", code="upload_failed") from exc
        finally:
            connection.close()
