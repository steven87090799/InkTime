"""OpenAI Responses adapter over InkTime's existing guarded Vision lifecycle."""
from __future__ import annotations

import logging
from typing import Any, cast

import requests

from .openai_compatible import OpenAICompatibleProvider, ProviderHTTPError
from .openai_models import OPENAI_MODELS, openai_model_capabilities


def responses_to_completion(payload: dict) -> dict:
    """Normalize only the wire envelope; leave JSON validation/scoring shared.

    Preserve usage on refusal/truncation, so a failed result is still accounted
    for without automatically buying another request or attempting JSON repair.
    """
    status = payload.get("status")
    output = payload.get("output")
    if status not in {"completed", "incomplete", "failed", "cancelled"} or not isinstance(output, list):
        raise ProviderHTTPError("Responses 回應缺少有效狀態或 output", "VLM-006", ambiguous=True)
    text, refusals = [], []
    message_incomplete = False
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise ProviderHTTPError("Responses message content 格式錯誤", "VLM-006", ambiguous=True)
        message_incomplete |= item.get("status") not in {None, "completed"}
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                text.append(part["text"])
            elif part.get("type") == "refusal":
                refusals.append(str(part.get("refusal") or "Provider refused"))
    incomplete = payload.get("incomplete_details")
    if incomplete is not None and not isinstance(incomplete, dict):
        raise ProviderHTTPError("Responses incomplete_details 格式錯誤", "VLM-006", ambiguous=True)
    reason = (incomplete or {}).get("reason")
    finish_reason = "stop" if status == "completed" else "length" if reason == "max_output_tokens" else "content_filter"
    if message_incomplete or (status == "completed" and not text and not refusals):
        finish_reason = "content_filter"
    return {
        "id": payload.get("id"), "model": payload.get("model"), "usage": payload.get("usage"),
        "choices": [{"finish_reason": finish_reason,
                     "message": {"content": "".join(text), "refusal": " ".join(refusals) or None}}],
    }


class _ResponsesSDKSession:
    """Use the official SDK without its automatic retries or environment keys.

    Management/Files keep the existing bounded, streaming Requests transport.
    The SDK raw HTTP response has the same status/JSON/text contract consumed
    by InkTime's tracing, consent fence and ambiguous-request protection.
    """
    def __init__(self, provider: OpenAICompatibleProvider) -> None:
        from openai import DefaultHttpxClient, OpenAI, Timeout

        # SDK DEBUG logging includes request bodies. Never allow it to bypass
        # InkTime's sanitized traces, even when the application enables DEBUG.
        for name in ("openai", "openai._base_client", "httpx", "httpx2", "httpcore", "httpcore2"):
            logging.getLogger(name).setLevel(logging.WARNING)
        self.client = OpenAI(
            api_key=provider.api_key or "missing-key",
            organization=str(provider.options.get("organization_id") or ""),
            project=str(provider.options.get("project_id") or ""),
            base_url=provider.base_url,
            max_retries=0,
            timeout=Timeout(provider.timeout, connect=min(10.0, provider.timeout)),
            http_client=DefaultHttpxClient(follow_redirects=False),
        )
        # Passing empty strings prevents SDK environment fallback; clear them
        # after construction so optional headers are omitted rather than empty.
        if not provider.options.get("organization_id"):
            self.client.organization = None
        if not provider.options.get("project_id"):
            self.client.project = None
        self.management = requests.Session()

    def post(self, url, **kwargs):
        if not url.endswith("/responses"):
            return self.management.post(url, **kwargs)
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        try:
            result = self.client.responses.with_raw_response.create(**kwargs["json"])
            return result.http_response
        except APIStatusError as exc:
            return exc.response
        except APITimeoutError:
            # Conservatively ambiguous even if the SDK cannot prove dispatch.
            raise requests.ReadTimeout("OpenAI timeout") from None
        except APIConnectionError:
            raise requests.ConnectionError("OpenAI connection failed") from None

    def get(self, url, **kwargs):
        return self.management.get(url, **kwargs)

    def delete(self, url, **kwargs):
        return self.management.delete(url, **kwargs)

    def close(self):
        self.client.close()
        self.management.close()


class OpenAIResponsesProvider(OpenAICompatibleProvider):
    completion_path = "/responses"
    api_mode = "responses"
    batch_endpoint = "/v1/responses"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        if self.kind != "openai":
            raise ValueError("PROVIDER-023 Responses Adapter 只用於 OpenAI 官方 API")
        if kwargs.get("session") is None:
            self.session.close()
            self.session = cast(Any, _ResponsesSDKSession(self))

    def _completion_payload(self, payload: dict) -> dict:
        return responses_to_completion(payload)

    def _post_completion(self, body: dict, **kwargs):
        # Keep synchronous cost estimates on Standard, even when the Project
        # defaults to Fast. Batch builders omit this request-tier override.
        body["service_tier"] = "default"
        return super()._post_completion(body, **kwargs)

    def _apply_provider_request_policy(
        self, body: dict[str, Any], *, model: str, stage: str, prompt_identity: str,
        reasoning_effort: str | None, provider_request_context_id: str | None,
        allow_reasoning: bool,
    ) -> dict[str, Any]:
        capabilities = openai_model_capabilities(model)
        efforts = capabilities["reasoning_efforts"]
        effort = self.options.get("reasoning_effort", reasoning_effort or "none")
        if not allow_reasoning:
            effort = "none" if "none" in efforts else "low"
        if effort not in efforts:
            raise ValueError(f"PROVIDER-023 {model} reasoning effort 只支援：{'、'.join(efforts)}")
        body.pop("temperature", None)
        if "max_tokens" in body:
            body["max_output_tokens"] = body.pop("max_tokens")
        inputs = body.pop("messages")
        for message in inputs:
            if isinstance(message["content"], list):
                converted = []
                for part in message["content"]:
                    if part["type"] == "text":
                        converted.append({"type": "input_text", "text": part["text"]})
                    elif part["type"] == "image_url":
                        converted.append({"type": "input_image", **part["image_url"],
                                          "image_url": part["image_url"]["url"]})
                        converted[-1].pop("url")
                message["content"] = converted
        response_format = body.pop("response_format", None)
        if response_format and response_format.get("type") == "json_schema":
            body["text"] = {"format": {"type": "json_schema", **response_format["json_schema"]}}
        else:
            body["text"] = {"format": {"type": "json_object"}}
        body.update(input=inputs, reasoning={"effort": effort}, store=False)
        return body

    def available_models(self) -> list[dict]:
        response = self._send("GET", "/models", headers=self._headers(), timeout=(10, 15))
        payload = self._json_response(response)
        data = payload.get("data")
        if not isinstance(data, list):
            raise ProviderHTTPError("OpenAI 模型清單格式錯誤", "VLM-006")
        account_ids = {item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)}
        return [{"id": model, "available": model in account_ids,
                 "reasoning_efforts": spec["reasoning_efforts"], "vision": True,
                 "structured_outputs": True, "batch": True}
                for model, spec in OPENAI_MODELS.items()]

    def validate_config(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "尚未設定 OpenAI API Key"
        models = self.available_models()
        selected = getattr(self, "configured_model", "")
        valid = any(item["available"] and (not selected or item["id"] == selected) for item in models)
        return valid, "OpenAI 連線與模型權限查驗完成；未呼叫圖片模型"


def create_vision_provider(**kwargs) -> OpenAICompatibleProvider:
    provider_class = (OpenAIResponsesProvider
                      if kwargs.get("kind") == "openai" and (kwargs.get("options") or {}).get("api_mode") == "responses"
                      else OpenAICompatibleProvider)
    return provider_class(**kwargs)
