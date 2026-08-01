from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    usage: Usage
    request_id: str | None = None


class VisionProvider(ABC):
    supports_reasoning_effort = False

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    def process_spec(self) -> dict | None:
        """Serializable child construction data, or None for cooperative timeout."""

        return None

    def build_analysis_request_body(
        self,
        *,
        image_path: Path,
        model: str,
        detail: str,
        stage: str,
        max_tokens: int | None = None,
        caption_controls: dict | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        raise NotImplementedError("Provider 未實作共用分析 Request Body Builder")

    def upload_batch_file(self, path: Path, *, remote_filename: str | None = None) -> str:
        raise NotImplementedError("Provider 不支援 Batch File Upload")

    def create_batch(
        self,
        input_file_id: str,
        *,
        completion_window: str = "24h",
        metadata: dict | None = None,
        output_expires_after_seconds: int | None = None,
    ) -> dict:
        raise NotImplementedError("Provider 不支援 Batch Create")

    def retrieve_batch(self, batch_id: str) -> dict:
        return self.poll_batch(batch_id)

    def retrieve_file(self, file_id: str) -> dict:
        raise NotImplementedError("Provider 不支援 Batch File Metadata Recovery")

    def download_file_content(self, file_id: str, destination: Path) -> Path:
        raise NotImplementedError("Provider 不支援 Batch File Download")

    def delete_remote_file(self, file_id: str) -> dict:
        raise NotImplementedError("Provider 不支援 Remote File Delete")

    def estimate_batch_cost(self, model: str, usage: Usage) -> float:
        """Use provider pricing when available; compatible providers may override."""

        return self.estimate_cost(model, usage)

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
        reasoning_effort: str | None = None,
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
