from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import requests

from inktime.app.domain.analysis.schema import ANALYSIS_JSON_SCHEMA
from inktime.app.domain.analysis.scoring import DEFAULT_SCORING_RULES
from .base import ProviderResponse, Usage, VisionProvider


SYSTEM_PROMPT = """你是 InkTime 個人照片分析器。只輸出符合指定 JSON Schema 的 JSON，不可使用 Markdown code fence。請以繁體中文（台灣用語）描述。一次完成內容描述、允許類型、回憶分數、美觀分、技術品質分、情緒分、電子紙短文案、保留建議、敏感內容判斷與簡短原因。不得虛構人物關係、身份、地點或事件。"""


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

    def _post_completion(self, body: dict) -> ProviderResponse:
        try:
            response = self.session.post(
                self._url("/chat/completions"), headers=self._headers(), json=body, timeout=self.timeout
            )
        except requests.Timeout as exc:
            raise ProviderHTTPError("Provider API 逾時", "VLM-001") from exc
        except requests.RequestException as exc:
            raise ProviderHTTPError("Provider 連線失敗", "VLM-001") from exc
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise ProviderHTTPError(
                "Provider Rate Limit",
                "VLM-002",
                float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if response.status_code >= 400:
            raise ProviderHTTPError(f"Provider 回應 HTTP {response.status_code}", "VLM-006")
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
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
            body["response_format"] = {"type": "json_schema", "json_schema": ANALYSIS_JSON_SCHEMA}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return self._post_completion(body)

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
        return self._post_completion(body)

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
        upload = self.session.post(
            self._url("/files"),
            headers=upload_headers,
            data={"purpose": "batch"},
            files={"file": ("inktime-batch.jsonl", content, "application/jsonl")},
            timeout=self.timeout,
        )
        if upload.status_code >= 400:
            raise ProviderHTTPError(f"Batch 檔案上傳失敗 HTTP {upload.status_code}", "VLM-007")
        input_file_id = upload.json()["id"]
        response = self.session.post(
            self._url("/batches"),
            headers=self._headers(),
            json={
                "input_file_id": input_file_id,
                "endpoint": "/v1/chat/completions",
                "completion_window": completion_window,
            },
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise ProviderHTTPError(f"Batch 建立失敗 HTTP {response.status_code}", "VLM-007")
        return str(response.json()["id"])

    def poll_batch(self, batch_id: str) -> dict:
        response = self.session.get(
            self._url(f"/batches/{batch_id}"), headers=self._headers(), timeout=self.timeout
        )
        if response.status_code >= 400:
            raise ProviderHTTPError(f"Batch 查詢失敗 HTTP {response.status_code}", "VLM-007")
        return dict(response.json())

    def cancel_batch(self, batch_id: str) -> dict:
        response = self.session.post(
            self._url(f"/batches/{batch_id}/cancel"), headers=self._headers(), timeout=self.timeout
        )
        if response.status_code >= 400:
            raise ProviderHTTPError(f"Batch 取消失敗 HTTP {response.status_code}", "VLM-007")
        return dict(response.json())

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
                self._url("/models"), headers=self._headers(), timeout=min(self.timeout, 15)
            )
        except requests.RequestException as exc:
            return False, f"無法連線：{exc.__class__.__name__}"
        if response.status_code >= 400:
            return False, f"Provider 回應 HTTP {response.status_code}"
        return True, "連線成功"
