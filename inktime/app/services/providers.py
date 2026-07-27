from __future__ import annotations

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
        channels = []
        rules = str(self.settings.get("analysis.scoring_rules", "")) if scoring_rules is None else str(scoring_rules)
        summaries = {str(item["id"]): item for item in self.repository.list()}
        requested = route_snapshot or self.route_snapshot()
        for snapshot in requested:
            provider_id = str(snapshot.get("provider_id") or snapshot.get("id") or "")
            summary = summaries.get(provider_id)
            if summary is None:
                raise ValueError(f"VLM-008 Job 指定的 Provider 已刪除：{provider_id}")
            if not summary["enabled"]:
                raise ValueError(f"VLM-008 Job 指定的 Provider 已停用：{provider_id}")
            revision = str(snapshot.get("config_revision") or "")
            if revision and revision != str(summary.get("updated_at") or ""):
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

    def route_snapshot(self) -> list[dict]:
        """Allowlisted ordered routing identity for an Analysis Plan."""
        return [
            {
                "provider_id": str(row["id"]),
                "display_name": str(row.get("name") or row["id"]),
                "priority": int(row.get("priority") or 100),
                "config_revision": str(row.get("updated_at") or ""),
            }
            for row in sorted(
                (row for row in self.repository.list() if row["enabled"]),
                key=lambda row: (int(row.get("priority") or 100), str(row.get("name") or row["id"])),
            )
        ]
