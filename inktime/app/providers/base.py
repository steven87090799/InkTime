from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    usage: Usage
    request_id: str | None = None


class VisionProvider(ABC):
    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @abstractmethod
    def analyze(
        self,
        *,
        image_path: Path,
        model: str,
        detail: str,
        stage: str,
        max_tokens: int | None = None,
        caption_controls: dict | None = None,
    ) -> ProviderResponse:
        raise NotImplementedError

    @abstractmethod
    def repair_json(
        self,
        *,
        invalid_content: str,
        validation_error: str,
        model: str,
        max_tokens: int | None = None,
        stage: str = "single_high",
        caption_controls: dict | None = None,
    ) -> ProviderResponse:
        """只傳文字修復 JSON，不得再次上傳圖片。"""
        raise NotImplementedError

    @abstractmethod
    def submit_batch(self, requests: list[dict], *, completion_window: str = "24h") -> str:
        raise NotImplementedError

    @abstractmethod
    def poll_batch(self, batch_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def cancel_batch(self, batch_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def estimate_cost(self, model: str, usage: Usage) -> float:
        raise NotImplementedError

    @abstractmethod
    def validate_config(self) -> tuple[bool, str]:
        raise NotImplementedError
