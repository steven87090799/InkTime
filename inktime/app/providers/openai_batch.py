from __future__ import annotations

from .openai_provider import OpenAIProvider


class OpenAIBatchProvider(OpenAIProvider):
    """標示支援 Batch 的 OpenAI Provider；提交、查詢與取消沿用統一介面。"""

    supports_batch = True
