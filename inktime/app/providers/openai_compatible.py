from __future__ import annotations

import base64
from io import BytesIO
import json
import os
from pathlib import Path
import time
from typing import Any

import requests

from inktime.app.domain.analysis.schema import json_schema_for_stage
from inktime.app.domain.analysis.scoring import DEFAULT_SCORING_RULES
from .base import ProviderResponse, Usage, VisionProvider


SYSTEM_PROMPT = """你是 InkTime 個人照片分析器。只輸出符合指定 JSON Schema 的精簡 JSON，不可使用 Markdown code fence 或長篇敘述。請以繁體中文（台灣用語）描述。未知值使用 null 或 unknown；不得虛構人物關係、身份、地點或事件。完整 Schema 必須在同一次請求完成回憶、美學、技術、情緒、顯示適合度、場景、主體、裁切、電子紙與搜尋資訊；文案、地標與電子紙資訊不得再另行呼叫模型。評分等級使用 S/A/B/C/D/E，程式會換算排序分。visual_orientation 的基準是圖片已套用 EXIF transpose 後，尚需順時針旋轉多少度才正立；只能填 0/90/180/270/null。無可靠視覺線索時 rotation_cw=null、ambiguous=true 且 evidence 僅為 insufficient_visual_cues。side_caption 必須是繁體中文的一句短文案，不換行、不加引號，8 至 16 字（目標 12 字），有生活感或含蓄畫面感；不得虛構故事，不得以「這是一張」「這張照片」「照片中」「畫面中」起句，也不可只是客觀重述人物、物件或活動。"""


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
        self.base_url = self.normalize_base_url(base_url)
        self.api_key = api_key
        self.pricing = pricing or {}
        self.timeout = timeout
        self.request_timeout = (min(10.0, timeout), timeout)
        self.supports_json_schema = supports_json_schema
        self.scoring_rules = scoring_rules.strip() or DEFAULT_SCORING_RULES
        self.caption_controls = dict(caption_controls or {})
        self.session = session or requests.Session()

    def process_spec(self) -> dict[str, Any]:
        return {
            "provider_kind": "openai_compatible",
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "timeout": self.timeout,
            "supports_json_schema": self.supports_json_schema,
            "scoring_rules": self.scoring_rules,
            "caption_controls": self.caption_controls,
        }

    @classmethod
    def from_process_spec(cls, specification: dict[str, Any]):
        if str(specification.get("provider_kind")) != "openai_compatible":
            raise ValueError("unsupported provider process specification")
        return cls(
            name=str(specification["name"]),
            base_url=str(specification["base_url"]),
            api_key=str(specification.get("api_key", "")),
            timeout=float(specification.get("timeout", 120)),
            supports_json_schema=bool(specification.get("supports_json_schema", True)),
            scoring_rules=str(specification.get("scoring_rules", DEFAULT_SCORING_RULES)),
            caption_controls=dict(specification.get("caption_controls") or {}),
        )

    def close(self) -> None:
        self.session.close()

    @staticmethod
    def normalize_base_url(base_url: str) -> str:
        value = str(base_url or "").strip().rstrip("/")
        for suffix in ("/chat/completions", "/batches"):
            if value.endswith(suffix):
                value = value[: -len(suffix)].rstrip("/")
        if not value:
            raise ValueError("Provider Base URL 不可空白")
        return value

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
        completion_details = usage.get("completion_tokens_details") or {}
        return Usage(
            input_tokens=int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
            output_tokens=int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
            cached_tokens=int(details.get("cached_tokens", 0) or 0),
            reasoning_tokens=int(completion_details.get("reasoning_tokens", 0) or 0),
        )

    def _redact(self, message: str) -> str:
        value = str(message)
        if self.api_key:
            value = value.replace(self.api_key, "[REDACTED]")
        return value.replace("Authorization", "[REDACTED-AUTHORIZATION]")

    @staticmethod
    def _retry_after(response) -> float | None:
        value = response.headers.get("Retry-After") if hasattr(response, "headers") else None
        try:
            return max(0.0, float(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _send(self, method: str, path: str, **kwargs):
        last_response = None
        for attempt in range(3):
            try:
                sender = getattr(self.session, method.lower())
                response = sender(self._url(path), **kwargs)
            except requests.Timeout as exc:
                if attempt == 2:
                    raise ProviderHTTPError("Provider API 逾時", "VLM-001") from exc
                time.sleep(min(1.0, 0.1 * (attempt + 1)))
                continue
            except requests.RequestException as exc:
                if attempt == 2:
                    raise ProviderHTTPError("Provider 連線失敗", "VLM-001") from exc
                time.sleep(min(1.0, 0.1 * (attempt + 1)))
                continue
            last_response = response
            status = int(getattr(response, "status_code", 0) or 0)
            if status == 429 or status >= 500:
                if attempt == 2:
                    code = "VLM-002" if status == 429 else "VLM-007"
                    raise ProviderHTTPError(
                        self._redact(f"Provider 回應 HTTP {status}"), code, self._retry_after(response)
                    )
                delay = self._retry_after(response) or min(1.0, 0.1 * (attempt + 1))
                time.sleep(min(30.0, delay))
                continue
            return response
        raise ProviderHTTPError("Provider API 重試失敗", "VLM-007") from last_response

    def _json_response(self, response, *, error_code: str = "VLM-007") -> dict:
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            raise ProviderHTTPError(
                self._redact(f"Provider 回應 HTTP {status}"), error_code, self._retry_after(response)
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderHTTPError("Provider 回應不是有效 JSON", error_code) from exc
        if not isinstance(payload, dict):
            raise ProviderHTTPError("Provider 回應必須是 JSON Object", error_code)
        return payload

    def _post_completion(self, body: dict) -> ProviderResponse:
        response = self._send("POST", "/chat/completions", headers=self._headers(), json=body, timeout=self.request_timeout)
        payload = self._json_response(response, error_code="VLM-006")
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderHTTPError("Provider 回應缺少有效 Response Body", "VLM-006") from exc
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return ProviderResponse(
            str(content).strip(), self._usage(payload), response.headers.get("x-request-id")
        )

    def build_analysis_request_body(
        self,
        *,
        image_path: Path,
        model: str,
        detail: str,
        stage: str,
        max_tokens: int | None = None,
        caption_controls: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                "json_schema": json_schema_for_stage(
                    stage, caption_controls=caption_controls or self.caption_controls
                ),
            }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return body

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
        body = self.build_analysis_request_body(
            image_path=image_path,
            model=model,
            detail=detail,
            stage=stage,
            max_tokens=max_tokens,
            caption_controls=caption_controls,
        )
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
                            "schema": json_schema_for_stage(
                                stage, caption_controls=caption_controls or self.caption_controls
                            )["schema"],
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
                "json_schema": json_schema_for_stage(
                    stage, caption_controls=caption_controls or self.caption_controls
                ),
            }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return self._post_completion(body)

    def submit_batch(self, requests: list[dict], *, completion_window: str = "24h") -> str:
        if not requests or len(requests) > 50_000:
            raise ValueError("單一 Batch 必須包含 1 到 50,000 個請求")
        content = BytesIO()
        for index, request in enumerate(requests):
            item = dict(request)
            item.setdefault("custom_id", f"inktime-{index}")
            item.setdefault("method", "POST")
            item.setdefault("url", "/v1/chat/completions")
            if "body" not in item:
                raise ValueError("Batch 每個請求都需要 body")
            content.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            content.write(b"\n")
        data = content.getvalue()
        if len(data) > 200 * 1024 * 1024:
            raise ValueError("Batch JSONL 不可超過 200 MB")
        upload_headers = {}
        if self.api_key:
            upload_headers["Authorization"] = f"Bearer {self.api_key}"
        upload = self._send(
            "POST",
            "/files",
            headers=upload_headers,
            data={"purpose": "batch"},
            files={"file": ("inktime-batch.jsonl", data, "application/jsonl")},
            timeout=self.request_timeout,
        )
        payload = self._json_response(upload)
        input_file_id = payload.get("id")
        if not input_file_id:
            raise ProviderHTTPError("Batch 上傳回應缺少 file id", "VLM-007")
        return str(self.create_batch(str(input_file_id), completion_window=completion_window)["id"])

    def upload_batch_file(self, path: Path) -> str:
        if not path.is_file():
            raise FileNotFoundError("BATCH-FILE-001 找不到本機 JSONL")
        with path.open("rb") as stream:
            response = self._send(
                "POST",
                "/files",
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                data={"purpose": "batch"},
                files={"file": (path.name, stream, "application/jsonl")},
                timeout=self.request_timeout,
            )
        payload = self._json_response(response)
        file_id = payload.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise ProviderHTTPError("Batch 上傳回應缺少 file id", "VLM-007")
        return file_id

    def create_batch(
        self,
        input_file_id: str,
        *,
        completion_window: str = "24h",
        metadata: dict | None = None,
        output_expires_after_seconds: int | None = None,
    ) -> dict:
        if not input_file_id:
            raise ValueError("BATCH-REMOTE-002 input_file_id 不可空白")
        body: dict[str, Any] = {
            "input_file_id": input_file_id,
            "endpoint": "/v1/chat/completions",
            "completion_window": completion_window,
        }
        if metadata:
            body["metadata"] = {
                str(key): str(value)[:120]
                for key, value in metadata.items()
                if str(key)[:64].replace("_", "").isalnum()
            }
        if output_expires_after_seconds is not None:
            body["output_expires_after"] = {"seconds": max(3600, int(output_expires_after_seconds))}
        response = self._send("POST", "/batches", headers=self._headers(), json=body, timeout=self.request_timeout)
        payload = self._json_response(response)
        if not isinstance(payload.get("id"), str) or not payload["id"]:
            raise ProviderHTTPError("Batch 建立回應缺少 batch id", "VLM-007")
        return payload

    def poll_batch(self, batch_id: str) -> dict:
        return self.retrieve_batch(batch_id)

    def retrieve_batch(self, batch_id: str) -> dict:
        if not batch_id:
            raise ValueError("BATCH-REMOTE-003 batch_id 不可空白")
        response = self._send(
            "GET", f"/batches/{batch_id}", headers=self._headers(), timeout=self.request_timeout
        )
        return self._json_response(response)

    def cancel_batch(self, batch_id: str) -> dict:
        response = self._send(
            "POST", f"/batches/{batch_id}/cancel", headers=self._headers(), timeout=self.request_timeout
        )
        return self._json_response(response)

    def download_file_content(self, file_id: str, destination: Path) -> Path:
        if not file_id:
            raise ValueError("BATCH-FILE-002 file_id 不可空白")
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        temporary.unlink(missing_ok=True)
        try:
            response = self._send(
                "GET",
                f"/files/{file_id}/content",
                headers=self._headers(),
                timeout=self.request_timeout,
                stream=True,
            )
            if int(response.status_code) >= 400:
                self._json_response(response)
            with temporary.open("wb") as stream:
                os.chmod(temporary, 0o600)
                iterator = response.iter_content(chunk_size=1024 * 1024)
                for chunk in iterator:
                    if chunk:
                        stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
            return destination
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, ProviderHTTPError):
                raise
            raise ProviderHTTPError("Batch 檔案下載中斷", "BATCH-FILE-003") from exc

    def delete_remote_file(self, file_id: str) -> dict:
        if not file_id:
            raise ValueError("BATCH-FILE-004 file_id 不可空白")
        response = self._send(
            "DELETE", f"/files/{file_id}", headers=self._headers(), timeout=self.request_timeout
        )
        return self._json_response(response)

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
                self._url("/models"),
                headers=self._headers(),
                timeout=(min(10.0, self.timeout), min(self.timeout, 15)),
            )
        except requests.RequestException as exc:
            return False, f"無法連線：{exc.__class__.__name__}"
        if response.status_code >= 400:
            return False, f"Provider 回應 HTTP {response.status_code}"
        return True, "連線成功"
