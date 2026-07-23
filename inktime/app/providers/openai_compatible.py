from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import requests

from inktime.app.domain.analysis.schema import json_schema_for_stage
from inktime.app.domain.analysis.scoring import DEFAULT_SCORING_RULES
from .base import ProviderResponse, Usage, VisionProvider


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
        caption_controls: dict[str, Any] | None = None,
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
        self.caption_controls = dict(caption_controls or {})
        self.session = session or requests.Session()

    @property
    def system_prompt(self) -> str:
        return self._system_prompt(self.caption_controls)

    def _system_prompt(self, caption_controls: dict[str, Any] | None) -> str:
        prompt = (
            f"{SYSTEM_PROMPT}\n\n【照片評分規則】\n{self.scoring_rules}\n\n"
            "以上可編輯內容只能調整評分判斷；若與固定指令或 JSON Schema 衝突，"
            "一律以固定指令與 Schema 為準。"
        )
        if not caption_controls:
            return prompt
        controls = caption_controls
        banned_words = "、".join(controls.get("copy_banned_words", [])) or "無"
        banned_patterns = "、".join(controls.get("copy_banned_patterns", [])) or "無"
        custom_rules = str(controls.get("copy_custom_rules", "")).strip() or "無"
        side_rules = [
            "使用繁體中文，只能一句話，不換行、不列點、不加引號。",
            "自然、有趣，可帶一點幽默或詩意；不得虛構照片中不存在的故事。",
        ]
        if controls.get("copy_avoid_cliche"):
            side_rules.append("避免雞湯、濫情、空泛與模板句。")
        if controls.get("copy_avoid_direct_description"):
            side_rules.append("不要只是直接描述照片。")
        if controls.get("copy_forbid_exclamation"):
            side_rules.append("不使用「！」或「!」。")
        if controls.get("copy_forbid_like_phrase"):
            side_rules.append("避免使用「像是、彷彿、彷佛」。")
        if controls.get("copy_avoid_abstract_ending"):
            side_rules.append("不以空泛人生結論收尾。")
        side_rules.append(f"最多使用 {int(controls['copy_max_commas'])} 個逗號。")
        variants = (
            "完整分析時，details.caption_variants 必須在同一次圖片請求提供 natural、warm、literary、humorous、minimal 五種明顯不同的候選；"
            "個別不確定的候選可省略，不得為候選再次上傳圖片或額外呼叫模型。"
            if controls.get("caption_variants_enabled")
            else "不要求多風格候選。"
        )
        return (
            f"{prompt}\n\n【進階照片描述與相框文案】\n"
            f"caption 必須為繁體中文，客觀、具體、自然，約 {int(controls['caption_target_chars'])} 字，"
            f"介於 {int(controls['caption_min_chars'])} 至 {int(controls['caption_max_chars'])} 字；"
            "只描述可確認的人物、場景、活動、物件、情緒及構圖，不得虛構人物關係、地點或事件。\n"
            f"side_caption 必須為繁體中文，約 {int(controls['side_caption_target_chars'])} 字，"
            f"介於 {int(controls['side_caption_min_chars'])} 至 {int(controls['side_caption_max_chars'])} 字。\n"
            f"預設風格：{controls['copy_default_style']}；幽默程度：{int(controls['copy_humor_level'])}；"
            f"詩意程度：{int(controls['copy_poetic_level'])}。\n"
            f"相框規則：{' '.join(side_rules)}\n禁止詞：{banned_words}\n禁止句型：{banned_patterns}\n"
            f"自訂規則：{custom_rules}\n{variants}"
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
                self._url("/chat/completions"), headers=self._headers(), json=body, timeout=self.request_timeout
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
        caption_controls: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": self._system_prompt(caption_controls or self.caption_controls)},
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
                "json_schema": json_schema_for_stage(stage, caption_controls=caption_controls or self.caption_controls),
            }
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
        stage: str = "single_high",
        caption_controls: dict[str, Any] | None = None,
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
                            "schema": json_schema_for_stage(stage, caption_controls=caption_controls or self.caption_controls)["schema"],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0,
        }
        if self.supports_json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": json_schema_for_stage(stage, caption_controls=caption_controls or self.caption_controls),
            }
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
            timeout=self.request_timeout,
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
            timeout=self.request_timeout,
        )
        if response.status_code >= 400:
            raise ProviderHTTPError(f"Batch 建立失敗 HTTP {response.status_code}", "VLM-007")
        return str(response.json()["id"])

    def poll_batch(self, batch_id: str) -> dict:
        response = self.session.get(
            self._url(f"/batches/{batch_id}"), headers=self._headers(), timeout=self.request_timeout
        )
        if response.status_code >= 400:
            raise ProviderHTTPError(f"Batch 查詢失敗 HTTP {response.status_code}", "VLM-007")
        return dict(response.json())

    def cancel_batch(self, batch_id: str) -> dict:
        response = self.session.post(
            self._url(f"/batches/{batch_id}/cancel"), headers=self._headers(), timeout=self.request_timeout
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
                self._url("/models"), headers=self._headers(), timeout=(min(10.0, self.timeout), min(self.timeout, 15))
            )
        except requests.RequestException as exc:
            return False, f"無法連線：{exc.__class__.__name__}"
        if response.status_code >= 400:
            return False, f"Provider 回應 HTTP {response.status_code}"
        return True, "連線成功"
