from __future__ import annotations

from .openai_compatible import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI 即時 API；與相容端點共用嚴格 Schema 與 usage 解析。"""

    def __init__(self, *, api_key: str, pricing=None, timeout: float = 120) -> None:
        super().__init__(
            name="OpenAI",
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            pricing=pricing,
            timeout=timeout,
            supports_json_schema=True,
        )
