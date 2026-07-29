"""DNS-pinned HTTPS transport for outbound Webhooks."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import json
import math
import os
import socket
import ssl
from typing import Any, Callable, Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit


class UnsafeWebhookURL(ValueError):
    pass


class WebhookTimeoutError(OSError):
    code = "webhook_timeout"


class WebhookConnectTimeout(WebhookTimeoutError):
    code = "webhook_connect_timeout"


class WebhookReadTimeout(WebhookTimeoutError):
    code = "webhook_read_timeout"


def _normalized_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or not address.is_global
    )


def _allowlist_values() -> tuple[str, ...]:
    return tuple(
        item.strip() for item in os.environ.get("INKTIME_WEBHOOK_ALLOWLIST", "").split(",") if item.strip()
    )


def _allowlisted(
    host: str,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    values: Iterable[str],
) -> bool:
    for raw in values:
        item = raw.rstrip(".").casefold()
        if item.startswith("."):
            if host.endswith(item) and host != item[1:]:
                return True
            continue
        if item == host:
            return True
        try:
            if address in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


@dataclass(frozen=True)
class ResolvedWebhookTarget:
    url: str
    hostname: str
    port: int
    request_target: str
    addresses: tuple[str, ...]


Resolver = Callable[..., list[tuple[Any, ...]]]


def resolve_webhook_destination(
    url: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
    allowlist: Iterable[str] | None = None,
) -> ResolvedWebhookTarget:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise UnsafeWebhookURL("WEBHOOK-SSRF-001 Webhook Port 不合法") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise UnsafeWebhookURL("WEBHOOK-SSRF-001 Webhook URL 必須是沒有帳密與 Fragment 的 HTTPS URL")
    if not 1 <= port <= 65535:
        raise UnsafeWebhookURL("WEBHOOK-SSRF-001 Webhook Port 不合法")
    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise UnsafeWebhookURL("WEBHOOK-SSRF-001 Webhook Hostname 不合法") from exc
    if not hostname or len(hostname) > 253:
        raise UnsafeWebhookURL("WEBHOOK-SSRF-001 Webhook Hostname 不合法")
    if all(character in "0123456789." for character in hostname):
        try:
            ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise UnsafeWebhookURL("WEBHOOK-SSRF-001 不接受模糊 IP 表示法") from exc
    try:
        records = resolver(hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, TimeoutError, OSError) as exc:
        raise UnsafeWebhookURL("WEBHOOK-SSRF-002 Webhook 網域無法解析") from exc
    addresses: list[str] = []
    configured = tuple(allowlist) if allowlist is not None else _allowlist_values()
    for record in records:
        try:
            address = _normalized_ip(str(record[4][0]))
        except (IndexError, ValueError) as exc:
            raise UnsafeWebhookURL("WEBHOOK-SSRF-002 Webhook DNS 回應不合法") from exc
        if _blocked(address) and not _allowlisted(hostname, address, configured):
            raise UnsafeWebhookURL("WEBHOOK-SSRF-003 Webhook 不可解析至內部或保留網路")
        canonical = str(address)
        if canonical not in addresses:
            addresses.append(canonical)
    if not addresses:
        raise UnsafeWebhookURL("WEBHOOK-SSRF-002 Webhook 網域沒有可用位址")
    normalized = SplitResult(
        "https",
        f"{hostname}:{port}" if port != 443 else hostname,
        parsed.path or "/",
        parsed.query,
        "",
    )
    request_target = parsed.path or "/"
    if parsed.query:
        request_target += f"?{parsed.query}"
    return ResolvedWebhookTarget(
        urlunsplit(normalized),
        hostname,
        port,
        request_target,
        tuple(addresses),
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        target: ResolvedWebhookTarget,
        address: str,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(
            target.hostname,
            target.port,
            timeout=timeout,
            context=context,
        )
        self._pinned_address = address
        self._ssl_context = context

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
        )
        peer = _normalized_ip(str(raw_socket.getpeername()[0]))
        if peer != _normalized_ip(self._pinned_address):
            raw_socket.close()
            raise UnsafeWebhookURL("WEBHOOK-SSRF-004 實際連線位址與驗證位址不一致")
        self.sock = self._ssl_context.wrap_socket(
            raw_socket,
            server_hostname=self.host,
        )
        tls_peer = _normalized_ip(str(self.sock.getpeername()[0]))
        if tls_peer != _normalized_ip(self._pinned_address):
            self.close()
            raise UnsafeWebhookURL("WEBHOOK-SSRF-004 TLS Peer 位址與驗證位址不一致")


@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    headers: dict[str, str]


ConnectionFactory = Callable[
    [ResolvedWebhookTarget, str, float, ssl.SSLContext],
    http.client.HTTPSConnection,
]


def _connection_factory(
    target: ResolvedWebhookTarget,
    address: str,
    timeout: float,
    context: ssl.SSLContext,
) -> http.client.HTTPSConnection:
    return _PinnedHTTPSConnection(
        target,
        address,
        timeout=timeout,
        context=context,
    )


class PinnedWebhookTransport:
    """Requests-like transport that never performs DNS after validation."""

    def __init__(
        self,
        *,
        resolver: Resolver = socket.getaddrinfo,
        connection_factory: ConnectionFactory = _connection_factory,
        ssl_context: ssl.SSLContext | None = None,
        allowlist: Iterable[str] | None = None,
    ) -> None:
        self.resolver = resolver
        self.connection_factory = connection_factory
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.allowlist = tuple(allowlist) if allowlist is not None else None

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: tuple[float, float],
        allow_redirects: bool,
    ) -> WebhookResponse:
        if allow_redirects:
            raise ValueError("Webhook transport 不允許 Redirect")
        if (
            not isinstance(timeout, tuple)
            or len(timeout) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
                for value in timeout
            )
        ):
            raise ValueError("Webhook timeout 必須包含正數 connect/read 秒數")
        connect_timeout, read_timeout = map(float, timeout)
        target = resolve_webhook_destination(
            url,
            resolver=self.resolver,
            allowlist=self.allowlist,
        )
        body = json_module_dumps(json)
        request_headers = dict(headers)
        request_headers["Content-Length"] = str(len(body))
        request_headers["Host"] = (
            target.hostname if target.port == 443 else f"{target.hostname}:{target.port}"
        )
        last_error: OSError | http.client.HTTPException | None = None
        for address in target.addresses:
            connection = self.connection_factory(
                target,
                address,
                connect_timeout,
                self.ssl_context,
            )
            response: http.client.HTTPResponse | None = None
            try:
                try:
                    connection.connect()
                except (TimeoutError, socket.timeout) as exc:
                    last_error = WebhookConnectTimeout("Webhook TCP/TLS 連線逾時")
                    last_error.__cause__ = exc
                    continue
                if connection.sock is None:
                    last_error = OSError("Webhook 連線未建立 Socket")
                    continue
                connection.sock.settimeout(read_timeout)
                connection.request(
                    "POST",
                    target.request_target,
                    body=body,
                    headers=request_headers,
                )
                try:
                    response = connection.getresponse()
                    response.read(64 * 1024)
                except (TimeoutError, socket.timeout) as exc:
                    raise WebhookReadTimeout("Webhook 等待回應逾時") from exc
                return WebhookResponse(
                    int(response.status),
                    {key: value for key, value in response.getheaders()},
                )
            except WebhookReadTimeout:
                # The request may already have reached the destination. Trying
                # another pinned address could duplicate the webhook.
                raise
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
            finally:
                if response is not None:
                    response.close()
                connection.close()
        if last_error is not None:
            raise last_error
        raise OSError("Webhook 沒有可連線的已驗證位址")


def json_module_dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_webhook_url(url: str) -> str:
    """Compatibility helper for settings validation and callers without transport."""

    return resolve_webhook_destination(url).url
