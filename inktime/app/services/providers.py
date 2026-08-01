from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel
from inktime.app.repositories.providers import ProviderRepository
from inktime.app.repositories.settings import SettingsRepository


class ProviderService:
    def __init__(self, repository: ProviderRepository, settings: SettingsRepository) -> None:
        self.repository = repository
        self.settings = settings

    def build_router(
        self, route_snapshot: list[dict] | None = None, *, scoring_rules: str | None = None
    ) -> FailoverVisionProvider | None:
        # ``None`` preserves the legacy "use the current route" behavior.  An
        # explicit empty snapshot is a frozen decision that no Provider may be
        # used; it must never discover a Provider added after the Job was made.
        requested = self.route_snapshot() if route_snapshot is None else route_snapshot
        if not requested:
            return None
        channels = []
        rules = (
            str(self.settings.get("analysis.scoring_rules", ""))
            if scoring_rules is None
            else str(scoring_rules)
        )
        summaries = {str(item["id"]): item for item in self.repository.list()}
        for snapshot in requested:
            provider_id = str(snapshot.get("provider_id") or snapshot.get("id") or "")
            summary = summaries.get(provider_id)
            if summary is None:
                raise ValueError(f"VLM-008 Job 指定的 Provider 已刪除：{provider_id}")
            if not summary["enabled"]:
                raise ValueError(f"VLM-008 Job 指定的 Provider 已停用：{provider_id}")
            revision = str(snapshot.get("config_revision") or "")
            if revision and revision != self.config_revision(summary):
                raise ValueError(f"VLM-008 Job 指定的 Provider 設定已變更：{provider_id}")
            config = self.repository.get(provider_id, include_secret=True)
            if config is None:
                raise ValueError(f"VLM-008 找不到 Job 指定的 Provider：{provider_id}")
            provider: Any = OpenAICompatibleProvider(
                name=config["name"],
                base_url=config["base_url"],
                api_key=config.get("api_key", ""),
                pricing=self.repository.pricing(config["id"]),
                timeout=config["timeout_seconds"],
                supports_json_schema=bool(config["supports_json_schema"]),
                scoring_rules=rules,
            )
            provider.provider_id = provider_id
            provider.display_name = str(config["name"])
            channels.append(
                ProviderChannel(
                    provider=provider,
                    priority=int(snapshot.get("priority", config["priority"])),
                    max_concurrency=config["max_concurrency"],
                    requests_per_minute=config["rate_limit_rpm"],
                    tokens_per_minute=config["token_limit_tpm"],
                    cooldown_seconds=config["cooldown_seconds"],
                )
            )
        return FailoverVisionProvider(channels) if channels else None

    @staticmethod
    def config_revision(provider: dict[str, Any]) -> str:
        """Fingerprint only behavior-affecting, non-secret Provider fields."""
        fields = {
            "provider_id": str(provider.get("id") or provider.get("provider_id") or ""),
            "kind": str(provider.get("kind") or ""),
            "base_url": str(provider.get("base_url") or ""),
            "supports_vision": bool(provider.get("supports_vision")),
            "supports_batch": bool(provider.get("supports_batch")),
            "supports_json_schema": bool(provider.get("supports_json_schema")),
            "priority": int(provider.get("priority") or 0),
            "rate_limit_rpm": provider.get("rate_limit_rpm"),
            "token_limit_tpm": provider.get("token_limit_tpm"),
            "max_concurrency": int(provider.get("max_concurrency") or 0),
            "timeout_seconds": int(provider.get("timeout_seconds") or 0),
            "cooldown_seconds": int(provider.get("cooldown_seconds") or 0),
        }
        payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()

    def route_snapshot(self) -> list[dict]:
        """Allowlisted ordered routing identity for an Analysis Plan."""
        return [
            {
                "provider_id": str(row["id"]),
                "display_name": str(row.get("name") or row["id"]),
                "priority": int(row.get("priority") or 100),
                "config_revision": self.config_revision(row),
            }
            for row in sorted(
                (row for row in self.repository.list() if row["enabled"]),
                key=lambda row: (int(row.get("priority") or 100), str(row.get("name") or row["id"])),
            )
        ]
