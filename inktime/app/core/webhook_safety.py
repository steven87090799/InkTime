"""SSRF boundary for outbound Webhooks; validation is deliberately DNS-aware."""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


class UnsafeWebhookURL(ValueError):
    pass


def _allowlisted(host: str, address: ipaddress._BaseAddress) -> bool:
    values = [item.strip() for item in os.environ.get("INKTIME_WEBHOOK_ALLOWLIST", "").split(",") if item.strip()]
    for item in values:
        if item.casefold() == host.casefold():
            return True
        try:
            if address in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def validate_webhook_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeWebhookURL("WEBHOOK-SSRF-001 Webhook URL 必須是沒有帳密的 HTTP(S) URL")
    if parsed.port and not 1 <= parsed.port <= 65535:
        raise UnsafeWebhookURL("WEBHOOK-SSRF-001 Webhook Port 不合法")
    try:
        records = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeWebhookURL("WEBHOOK-SSRF-002 Webhook 網域無法解析") from exc
    addresses = {ipaddress.ip_address(record[4][0]) for record in records}
    for address in addresses:
        blocked = address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast or address.is_unspecified
        if blocked and not _allowlisted(parsed.hostname, address):
            raise UnsafeWebhookURL("WEBHOOK-SSRF-003 Webhook 不可解析至內部或保留網路")
    return parsed.geturl()
