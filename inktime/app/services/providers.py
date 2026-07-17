from __future__ import annotations

from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel
from inktime.app.repositories.providers import ProviderRepository


class ProviderService:
    def __init__(self, repository: ProviderRepository) -> None:
        self.repository = repository

    def build_router(self) -> FailoverVisionProvider | None:
        channels = []
        for summary in self.repository.list():
            if not summary["enabled"]:
                continue
            config = self.repository.get(summary["id"], include_secret=True)
            provider = OpenAICompatibleProvider(
                name=config["name"], base_url=config["base_url"], api_key=config.get("api_key", ""),
                pricing=self.repository.pricing(config["id"]), timeout=config["timeout_seconds"],
                supports_json_schema=bool(config["supports_json_schema"]),
            )
            channels.append(ProviderChannel(
                provider=provider, priority=config["priority"], max_concurrency=config["max_concurrency"],
                requests_per_minute=config["rate_limit_rpm"], cooldown_seconds=config["cooldown_seconds"],
            ))
        return FailoverVisionProvider(channels) if channels else None
