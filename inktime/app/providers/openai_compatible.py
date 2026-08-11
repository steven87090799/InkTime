from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
import re
import time
from typing import Any

import requests

from inktime.app.domain.analysis.schema import ANALYSIS_JSON_SCHEMA, json_schema_for_stage
from inktime.app.domain.analysis.scoring import DEFAULT_SCORING_RULES
from .base import ProviderResponse, Usage, VisionProvider
from inktime.app.core.logging import log_event, should_log_rate_limited


LOGGER = logging.getLogger("provider_transport")
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _log_debug(message: str, *, event: str, **fields) -> None:
    """Rate-limit successful transport phases without suppressing failures."""

    if not LOGGER.isEnabledFor(logging.DEBUG):
        return
    key = ":".join(
        (
            "provider",
            event,
            str(fields.get("provider") or "unknown")[:64],
            str(fields.get("model") or "default")[:64],
            str(fields.get("stage") or "default")[:64],
        )
    )
    if should_log_rate_limited(key, interval_seconds=1):
        log_event(LOGGER, logging.DEBUG, message, event=event, **fields)


def _log_failure(level: int, message: str, *, event: str, **fields) -> None:
    """Bound repeated upstream outages while retaining a representative trace."""

    key = ":".join(
        (
            "provider-failure",
            event,
            str(fields.get("provider") or "unknown")[:64],
            str(fields.get("model") or "default")[:64],
            str(fields.get("http_status") or 0),
        )
    )
    if should_log_rate_limited(key, interval_seconds=5):
        log_event(LOGGER, level, message, event=event, **fields)


SYSTEM_PROMPT = """你是 InkTime 個人照片分析器。只輸出符合指定 JSON Schema 的精簡 JSON，不可使用 Markdown code fence 或長篇敘述。請以繁體中文（台灣用語）描述。未知值使用 null 或 unknown；不得虛構人物關係、身份、地點或事件。完整 Schema 必須在同一次請求完成回憶、美學、技術、情緒、顯示適合度、場景、主體、裁切、電子紙與搜尋資訊；文案、地標與電子紙資訊不得再另行呼叫模型。評分等級使用 S/A/B/C/D/E，程式會換算排序分。"""


class ProviderHTTPError(RuntimeError):
    def __init__(self, message: str, code: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class OpenAICompatibleProvider(VisionProvider):
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        pricing: dict[str, dict[str, float]] | None = None,
        timeout: float = 120,
        supports_json_schema: bool = True,
        scoring_rules: str = DEFAULT_SCORING_RULES,
        session: requests.Session | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.pricing = pricing or {}
        self.timeout = timeout
        self.request_timeout = (min(10.0, timeout), timeout)
        self.supports_json_schema = supports_json_schema
        self.scoring_rules = scoring_rules.strip() or DEFAULT_SCORING_RULES
        self.session = session or requests.Session()

    @property
    def system_prompt(self) -> str:
        return (
            f"{SYSTEM_PROMPT}\n\n【照片評分規則】\n{self.scoring_rules}\n\n"
            "以上可編輯內容只能調整評分判斷；若與固定指令或 JSON Schema 衝突，"
            "一律以固定指令與 Schema 為準。"
        )

    def _url(self, path: str) -> str:
        if self.base_url.endswith("/chat/completions") and path == "/chat/completions":
            return self.base_url
        return self.base_url + path

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _usage(payload: dict) -> Usage:
        usage = payload.get("usage") or {}
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        return Usage(
            input_tokens=int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
            output_tokens=int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
            cached_tokens=int(details.get("cached_tokens", 0) or 0),
        )

    @staticmethod
    def _provider_request_id(response: requests.Response) -> str:
        value = str(
            response.headers.get("x-request-id")
            or response.headers.get("x-openrouter-request-id")
            or ""
        ).strip()
        return value if SAFE_REQUEST_ID.fullmatch(value) else ""

    def _post_completion(
        self,
        body: dict,
        *,
        operation: str = "chat_completion",
        stage: str = "",
    ) -> ProviderResponse:
        model = str(body.get("model") or "")[:256]
        started = time.monotonic()
        _log_debug(
            "Provider request prepared",
            event="provider_request_prepared",
            provider=self.name,
            model=model,
            operation=operation,
            stage=stage,
            details={"path_category": "chat_completions", "request_started": False},
        )
        _log_debug(
            "Provider request started",
            event="provider_request_started",
            provider=self.name,
            model=model,
            operation=operation,
            stage=stage,
            details={"path_category": "chat_completions", "request_started": True},
        )
        try:
            response = self.session.post(
                self._url("/chat/completions"), headers=self._headers(), json=body, timeout=self.request_timeout
            )
        except requests.Timeout as exc:
            _log_failure(
                logging.WARNING,
                "Provider request timed out",
                event="provider_timeout",
                error_code="VLM-001",
                provider=self.name,
                model=model,
                operation=operation,
                stage=stage,
                duration_ms=int((time.monotonic() - started) * 1000),
                failure_class=type(exc).__name__,
                retryable=True,
                ambiguous=True,
            )
            raise ProviderHTTPError("Provider API 逾時", "VLM-001") from exc
        except requests.RequestException as exc:
            _log_failure(
                logging.WARNING,
                "Provider connection failed",
                event="provider_connection_error",
                error_code="VLM-001",
                provider=self.name,
                model=model,
                operation=operation,
                stage=stage,
                duration_ms=int((time.monotonic() - started) * 1000),
                failure_class=type(exc).__name__,
                retryable=True,
                ambiguous=True,
            )
            raise ProviderHTTPError("Provider 連線失敗", "VLM-001") from exc
        provider_request_id = self._provider_request_id(response)
        _log_debug(
            "Provider response received",
            event="provider_response_received",
            provider=self.name,
            model=model,
            operation=operation,
            stage=stage,
            http_status=int(response.status_code),
            provider_request_id=provider_request_id,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            _log_failure(
                logging.WARNING,
                "Provider rate limit response",
                event="provider_rate_limited",
                error_code="VLM-002",
                provider=self.name,
                model=model,
                operation=operation,
                stage=stage,
                http_status=429,
                provider_request_id=provider_request_id,
                retryable=True,
                details={"retry_after": retry_after if str(retry_after or "").isdigit() else ""},
            )
            raise ProviderHTTPError(
                "Provider Rate Limit",
                "VLM-002",
                float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if response.status_code >= 400:
            _log_failure(
                logging.WARNING if response.status_code >= 500 else logging.ERROR,
                "Provider returned an HTTP error",
                event="provider_server_error" if response.status_code >= 500 else "provider_protocol_error",
                error_code="VLM-006",
                provider=self.name,
                model=model,
                operation=operation,
                stage=stage,
                http_status=int(response.status_code),
                provider_request_id=provider_request_id,
                retryable=response.status_code >= 500,
            )
            raise ProviderHTTPError(f"Provider 回應 HTTP {response.status_code}", "VLM-006")
        try:
            payload = response.json()
        except ValueError as exc:
            _log_failure(
                logging.ERROR,
                "Provider response was not valid JSON",
                event="provider_invalid_json",
                error_code="VLM-006",
                provider=self.name,
                model=model,
                operation=operation,
                stage=stage,
                http_status=int(response.status_code),
                provider_request_id=provider_request_id,
                failure_class=type(exc).__name__,
                retryable=False,
            )
            raise
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            _log_failure(
                logging.ERROR,
                "Provider response schema was invalid",
                event="provider_schema_error",
                error_code="VLM-006",
                provider=self.name,
                model=model,
                operation=operation,
                stage=stage,
                http_status=int(response.status_code),
                provider_request_id=provider_request_id,
                failure_class=type(exc).__name__,
                retryable=False,
            )
            raise
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        _log_debug(
            "Provider request completed",
            event="provider_request_completed",
            provider=self.name,
            model=model,
            operation=operation,
            stage=stage,
            http_status=int(response.status_code),
            provider_request_id=provider_request_id,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return ProviderResponse(
            str(content).strip(), self._usage(payload), response.headers.get("x-request-id")
        )

    def analyze(
        self,
        *,
        image_path: Path,
        model: str,
        detail: str,
        stage: str,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"分析階段：{stage}。請分析這張照片。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": detail},
                        },
                    ],
                },
            ],
            "temperature": 0.1,
        }
        if self.supports_json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": json_schema_for_stage(stage),
            }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return self._post_completion(body, operation="analyze", stage=stage)

    def repair_json(
        self,
        *,
        invalid_content: str,
        validation_error: str,
        model: str,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        body = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "只修復 JSON 使其符合提供的 Schema；不可新增圖片推測，不可輸出 Markdown。",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "invalid_json": invalid_content[:12000],
                            "error": validation_error,
                            "schema": ANALYSIS_JSON_SCHEMA["schema"],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0,
        }
        if self.supports_json_schema:
            body["response_format"] = {"type": "json_schema", "json_schema": ANALYSIS_JSON_SCHEMA}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return self._post_completion(body, operation="json_repair", stage="json_repair")

    def submit_batch(self, requests: list[dict], *, completion_window: str = "24h") -> str:
        if not requests or len(requests) > 50_000:
            raise ValueError("單一 Batch 必須包含 1 到 50,000 個請求")
        lines = []
        for index, request in enumerate(requests):
            item = dict(request)
            item.setdefault("custom_id", f"inktime-{index}")
            item.setdefault("method", "POST")
            item.setdefault("url", "/v1/chat/completions")
            if "body" not in item:
                raise ValueError("Batch 每個請求都需要 body")
            lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        content = ("\n".join(lines) + "\n").encode("utf-8")
        if len(content) > 200 * 1024 * 1024:
            raise ValueError("Batch JSONL 不可超過 200 MB")
        upload_headers = {}
        if self.api_key:
            upload_headers["Authorization"] = f"Bearer {self.api_key}"
        started = time.monotonic()
        _log_debug(
            "Batch upload started",
            event="batch_upload_started",
            provider=self.name,
            operation="batch_upload",
            details={"count": len(requests), "payload_bytes": len(content)},
        )
        try:
            upload = self.session.post(
                self._url("/files"),
                headers=upload_headers,
                data={"purpose": "batch"},
                files={"file": ("inktime-batch.jsonl", content, "application/jsonl")},
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "Batch upload result is ambiguous",
                event="batch_upload_ambiguous",
                error_code="VLM-007",
                provider=self.name,
                operation="batch_upload",
                duration_ms=int((time.monotonic() - started) * 1000),
                failure_class=type(exc).__name__,
                retryable=True,
                ambiguous=True,
            )
            raise
        if upload.status_code >= 400:
            log_event(
                LOGGER,
                logging.ERROR,
                "Batch upload returned an HTTP error",
                event="batch_upload_failed",
                error_code="VLM-007",
                provider=self.name,
                operation="batch_upload",
                http_status=int(upload.status_code),
                duration_ms=int((time.monotonic() - started) * 1000),
                retryable=upload.status_code >= 500,
            )
            raise ProviderHTTPError(f"Batch 檔案上傳失敗 HTTP {upload.status_code}", "VLM-007")
        input_file_id = upload.json()["id"]
        _log_debug(
            "Batch upload completed",
            event="batch_upload_completed",
            provider=self.name,
            operation="batch_upload",
            duration_ms=int((time.monotonic() - started) * 1000),
            details={"count": len(requests)},
        )
        submit_started = time.monotonic()
        _log_debug(
            "Batch submission started",
            event="batch_submit_started",
            provider=self.name,
            operation="batch_submit",
            details={"count": len(requests)},
        )
        try:
            response = self.session.post(
                self._url("/batches"),
                headers=self._headers(),
                json={
                    "input_file_id": input_file_id,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": completion_window,
                },
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "Batch submission result is ambiguous",
                event="batch_submit_ambiguous",
                error_code="VLM-007",
                provider=self.name,
                operation="batch_submit",
                duration_ms=int((time.monotonic() - submit_started) * 1000),
                failure_class=type(exc).__name__,
                retryable=False,
                ambiguous=True,
            )
            raise
        if response.status_code >= 400:
            log_event(
                LOGGER,
                logging.ERROR,
                "Batch submission returned an HTTP error",
                event="batch_submit_failed",
                error_code="VLM-007",
                provider=self.name,
                operation="batch_submit",
                http_status=int(response.status_code),
                duration_ms=int((time.monotonic() - submit_started) * 1000),
                retryable=response.status_code >= 500,
                ambiguous=False,
            )
            raise ProviderHTTPError(f"Batch 建立失敗 HTTP {response.status_code}", "VLM-007")
        batch_id = str(response.json()["id"])
        log_event(
            LOGGER,
            logging.INFO,
            "Batch submitted",
            event="batch_submitted",
            provider=self.name,
            batch_id=batch_id,
            operation="batch_submit",
            duration_ms=int((time.monotonic() - submit_started) * 1000),
            details={"count": len(requests)},
        )
        return batch_id

    def poll_batch(self, batch_id: str) -> dict:
        started = time.monotonic()
        _log_debug(
            "Batch poll started",
            event="batch_poll_started",
            provider=self.name,
            batch_id=batch_id,
            operation="batch_poll",
        )
        response = self.session.get(
            self._url(f"/batches/{batch_id}"), headers=self._headers(), timeout=self.request_timeout
        )
        if response.status_code >= 400:
            raise ProviderHTTPError(f"Batch 查詢失敗 HTTP {response.status_code}", "VLM-007")
        result = dict(response.json())
        _log_debug(
            "Batch poll completed",
            event="batch_poll_completed",
            provider=self.name,
            batch_id=batch_id,
            operation="batch_poll",
            http_status=int(response.status_code),
            duration_ms=int((time.monotonic() - started) * 1000),
            details={"status": str(result.get("status") or "unknown")},
        )
        return result

    def cancel_batch(self, batch_id: str) -> dict:
        started = time.monotonic()
        response = self.session.post(
            self._url(f"/batches/{batch_id}/cancel"), headers=self._headers(), timeout=self.request_timeout
        )
        if response.status_code >= 400:
            raise ProviderHTTPError(f"Batch 取消失敗 HTTP {response.status_code}", "VLM-007")
        result = dict(response.json())
        log_event(
            LOGGER,
            logging.INFO,
            "Batch cancelled",
            event="batch_cancelled",
            provider=self.name,
            batch_id=batch_id,
            operation="batch_cancel",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result

    def estimate_cost(self, model: str, usage: Usage) -> float:
        price = self.pricing.get(model, {})
        uncached = max(0, usage.input_tokens - usage.cached_tokens)
        return (
            uncached * float(price.get("input_per_million", 0))
            + usage.cached_tokens
            * float(price.get("cached_input_per_million", price.get("input_per_million", 0)))
            + usage.output_tokens * float(price.get("output_per_million", 0))
        ) / 1_000_000

    def validate_config(self) -> tuple[bool, str]:
        try:
            response = self.session.get(
                self._url("/models"), headers=self._headers(), timeout=(min(10.0, self.timeout), min(self.timeout, 15))
            )
        except requests.RequestException as exc:
            _log_failure(
                logging.WARNING,
                "Provider configuration probe failed",
                event="provider_config_validation_failed",
                error_code="VLM-001",
                provider=self.name,
                operation="validate_config",
                failure_class=type(exc).__name__,
                retryable=True,
            )
            return False, f"無法連線：{exc.__class__.__name__}"
        if response.status_code >= 400:
            _log_failure(
                logging.WARNING,
                "Provider configuration probe returned an HTTP error",
                event="provider_config_validation_failed",
                error_code="VLM-006",
                provider=self.name,
                operation="validate_config",
                http_status=int(response.status_code),
                retryable=response.status_code >= 500,
            )
            return False, f"Provider 回應 HTTP {response.status_code}"
        return True, "連線成功"
