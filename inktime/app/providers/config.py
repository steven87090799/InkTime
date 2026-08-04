"""Provider kind, capability and network-policy contracts.

The API and the worker both use this module so a provider cannot be accepted
by the settings page and then interpreted differently by a child process.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
from typing import Any
from urllib.parse import urlparse


PROVIDER_KINDS = {"openai", "openrouter", "openai_compatible", "ollama"}
OPENROUTER_ROUTING_KEYS = (
    "order",
    "allow_fallbacks",
    "require_parameters",
    "data_collection",
    "zdr",
    "only",
    "ignore",
    "quantizations",
    "sort",
    "preferred_min_throughput",
    "preferred_max_latency",
    "max_price",
    "enforce_distillable_text",
)
OPENROUTER_OPTION_KEYS = set(OPENROUTER_ROUTING_KEYS) | {
    "http_referer",
    "app_title",
    "session_sticky",
    "allow_private_http",
}
GENERIC_OPTION_KEYS = {"allow_private_http"}


@dataclass(frozen=True)
class ProviderCapabilities:
    vision: bool
    batch: bool
    json_schema: bool
    reasoning: bool


def capabilities_for(kind: str, *, supports_json_schema: bool = True) -> ProviderCapabilities:
    normalized = str(kind or "").strip().lower()
    if normalized not in PROVIDER_KINDS:
        raise ValueError(f"PROVIDER-001 不支援的 Provider kind：{normalized or '空白'}")
    if normalized == "ollama":
        return ProviderCapabilities(True, False, bool(supports_json_schema), False)
    if normalized == "openrouter":
        return ProviderCapabilities(True, False, bool(supports_json_schema), True)
    return ProviderCapabilities(
        True,
        normalized in {"openai", "openai_compatible"},
        bool(supports_json_schema),
        normalized == "openai",
    )


def _bounded_string(value: Any, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > maximum
        or any(ord(char) < 0x20 for char in value)
    ):
        raise ValueError(f"PROVIDER-002 {field} 必須是 1 至 {maximum} 字元字串")
    return value.strip()


def _bounded_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 50 or any(not isinstance(item, str) for item in value):
        raise ValueError(f"PROVIDER-003 {field} 必須是最多 50 項的字串陣列")
    result = [_bounded_string(item, f"{field}[]", 100) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"PROVIDER-004 {field} 不可有重複值")
    return result


def _nonnegative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"PROVIDER-005 {field} 必須是非負數")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"PROVIDER-005 {field} 必須是有限非負數")
    return result


def _nonnegative_number_or_percentiles(value: Any, field: str) -> float | dict[str, float]:
    if not isinstance(value, dict):
        return _nonnegative_number(value, field)
    allowed = {"p50", "p75", "p90", "p99"}
    if not value or set(value) - allowed:
        raise ValueError(f"PROVIDER-005 {field} 必須是非負數或 p50/p75/p90/p99 object")
    return {str(name): _nonnegative_number(item, f"{field}.{name}") for name, item in value.items()}


def normalize_options(kind: str, options: Any) -> dict[str, Any]:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in PROVIDER_KINDS:
        raise ValueError(f"PROVIDER-001 不支援的 Provider kind：{normalized_kind or '空白'}")
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("PROVIDER-006 options 必須是 JSON object")
    allowed = OPENROUTER_OPTION_KEYS if normalized_kind == "openrouter" else GENERIC_OPTION_KEYS
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise ValueError(f"PROVIDER-007 options 含未知欄位：{', '.join(map(str, unknown))}")
    result: dict[str, Any] = {}
    for key, value in options.items():
        if key in {
            "allow_fallbacks",
            "require_parameters",
            "zdr",
            "session_sticky",
            "allow_private_http",
            "enforce_distillable_text",
        }:
            if type(value) is not bool:
                raise ValueError(f"PROVIDER-008 {key} 必須是 JSON Boolean")
            result[key] = value
        elif key in {"order", "only", "ignore", "quantizations"}:
            result[key] = _bounded_strings(value, key)
        elif key == "data_collection":
            if value not in {"allow", "deny"}:
                raise ValueError("PROVIDER-009 data_collection 必須是 allow 或 deny")
            result[key] = value
        elif key == "sort":
            if value not in {"price", "throughput", "latency"} and not (
                isinstance(value, dict) and set(value) <= {"by", "partition"}
            ):
                raise ValueError("PROVIDER-010 sort 必須是 price、throughput、latency 或合法 sort object")
            if isinstance(value, dict):
                if value.get("by") not in {"price", "throughput", "latency"}:
                    raise ValueError("PROVIDER-010 sort.by 不合法")
                if "partition" in value and value["partition"] not in {"none", "model", "provider"}:
                    raise ValueError("PROVIDER-010 sort.partition 不合法")
                result[key] = dict(value)
            else:
                result[key] = value
        elif key in {"preferred_min_throughput", "preferred_max_latency"}:
            result[key] = _nonnegative_number_or_percentiles(value, key)
        elif key == "max_price":
            allowed_price_fields = {"prompt", "completion", "request", "image"}
            if not isinstance(value, dict) or not value or set(value) - allowed_price_fields:
                raise ValueError("PROVIDER-011 max_price 必須是 prompt/completion/request/image 數字 object")
            result[key] = {str(name): _nonnegative_number(item, f"max_price.{name}") for name, item in value.items()}
        elif key in {"http_referer", "app_title"}:
            result[key] = _bounded_string(value, key, 512 if key == "http_referer" else 120)
        else:
            raise ValueError(f"PROVIDER-012 未處理的 options 欄位：{key}")
    if "order" in result and ("only" in result or "ignore" in result):
        raise ValueError("PROVIDER-013 OpenRouter order 不可與 only/ignore 同時設定")
    if set(result.get("only", [])) & set(result.get("ignore", [])):
        raise ValueError("PROVIDER-014 OpenRouter only 與 ignore 不可重疊")
    if "http_referer" in result:
        parsed = urlparse(result["http_referer"])
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("PROVIDER-015 http_referer 必須是 HTTPS URL")
    if normalized_kind == "openrouter":
        # Keep routing on endpoints that can honor the requested structured
        # output contract unless an administrator explicitly opts out.
        result.setdefault("require_parameters", True)
    return result


def canonical_options(kind: str, options: Any) -> str:
    return json.dumps(normalize_options(kind, options), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def is_openrouter_base_url(base_url: str) -> bool:
    """Detect an OpenRouter host for compatible-provider migration guidance."""

    try:
        hostname = (urlparse(str(base_url or "").strip()).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai")


def _is_private_host(host: str) -> bool:
    value = host.strip("[]").lower()
    try:
        address = ipaddress.ip_address(value)
        return address.is_private or address.is_loopback
    except ValueError:
        # Hostname resolution is intentionally not performed here.  A private
        # suffix can be DNS-rebound to a public address between validation and
        # the request, so generic HTTP accepts only literal private IPs.
        return False


def validate_base_url(
    kind: str,
    base_url: str,
    options: Any = None,
    *,
    require_private_http_option: bool = True,
) -> str:
    normalized_options = normalize_options(str(kind or "").strip().lower(), options)
    value = str(base_url or "").strip().rstrip("/")
    if any(ord(char) < 0x20 for char in value):
        raise ValueError("PROVIDER-016 base_url 不可包含控制字元")
    parsed = urlparse(value)
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError("PROVIDER-016 base_url 的 host 或 port 不合法") from exc
    if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        raise ValueError("PROVIDER-016 base_url 必須是沒有帳密的完整 http/https URL")
    if parsed.query or parsed.fragment:
        raise ValueError("PROVIDER-017 base_url 不可包含 query 或 fragment")
    if parsed.scheme == "http":
        if not _is_private_host(hostname):
            raise ValueError("PROVIDER-018 HTTP 只允許 literal private/loopback IP；請使用 HTTPS")
        if (
            require_private_http_option and not normalized_options.get("allow_private_http", False)
        ):
            raise ValueError("PROVIDER-019 私有網路 HTTP 必須明確設定 allow_private_http=true")
    return value
