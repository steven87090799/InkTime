from __future__ import annotations

from hashlib import sha256
from typing import Any
from inktime.app.domain.analysis.plan import analysis_output_token_cap

from inktime.app.domain.analysis.scoring import normalize_scoring_rules
from inktime.app.domain.jobs.failure_policy import JobConfigurationError
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.providers.openai_responses import create_vision_provider
from inktime.app.providers.config import (
    capabilities_for,
    provider_revision,
    provider_analysis_revision,
    effective_provider_kind,
    normalize_options,
    validate_model_id,
)
from inktime.app.providers.router import FailoverVisionProvider, ProviderChannel
from inktime.app.repositories.providers import ProviderRepository
from inktime.app.repositories.provider_quota import ProviderQuotaRepository
from inktime.app.repositories.settings import SettingsRepository


class ProviderService:
    def __init__(self, repository: ProviderRepository, settings: SettingsRepository) -> None:
        self.repository = repository
        self.settings = settings

    def build_router(
        self,
        route_snapshot: list[dict] | None = None,
        *,
        scoring_rules: str | None = None,
        caption_controls: dict | None = None,
    ) -> FailoverVisionProvider | None:
        # ``None`` preserves the legacy "use the current route" behavior.  An
        # explicit empty snapshot is a frozen decision that no Provider may be
        # used; it must never discover a Provider added after the Job was made.
        requested = self.route_snapshot() if route_snapshot is None else route_snapshot
        if not requested:
            return None
        channels = []
        rules = (
            normalize_scoring_rules(self.settings.get("analysis.scoring_rules", ""))
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
            if str(summary.get("usage_scope", "normal")) != str(snapshot.get("usage_scope", "normal")):
                raise ValueError("VLM-008 補齊專用 Provider 不可用於日常分析路由")
            revision = str(snapshot.get("config_revision") or "")
            if revision and revision != self.config_revision(summary):
                raise ValueError(f"VLM-008 Job 指定的 Provider 設定已變更：{provider_id}")
            config = self.repository.get(provider_id, include_secret=True)
            if config is None:
                raise ValueError(f"VLM-008 找不到 Job 指定的 Provider：{provider_id}")
            effective_kind = effective_provider_kind(
                str(config.get("kind") or "openai_compatible"), str(config.get("base_url") or "")
            )
            options = normalize_options(effective_kind, config.get("options") or {})
            pricing = self.repository.pricing(config["id"])
            if snapshot.get("complementary_lane") == "openrouter_daily":
                if effective_kind != "openrouter":
                    raise ValueError("每日免費分工僅可使用 OpenRouter")
                options = {**options, "free_only": True, "daily_free_first": False,
                           "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0}}
                pricing = {**pricing, str(config["model"]): {"input_per_million": 0, "cached_input_per_million": 0,
                                                           "output_per_million": 0, "batch_multiplier": 1}}
            provider: Any = create_vision_provider(
                name=config["name"],
                base_url=config["base_url"],
                api_key=config.get("api_key", ""),
                kind=effective_kind,
                provider_id=provider_id,
                options=options,
                pricing=pricing,
                timeout=config["timeout_seconds"],
                supports_json_schema=bool(config["supports_json_schema"]),
                scoring_rules=rules,
                caption_controls=caption_controls,
                supports_reasoning_effort=capabilities_for(
                    effective_kind,
                    supports_json_schema=bool(config["supports_json_schema"]),
                ).reasoning,
            )
            provider.provider_id = provider_id
            provider.display_name = str(config["name"])
            quota_identity = "|".join((provider.base_url, str(config.get("api_key") or ""),
                                       str(options.get("project_id") or config.get("project_id") or "")))
            if options.get("organization_id"):
                quota_identity += "|" + str(options["organization_id"])
            channels.append(
                ProviderChannel(
                    provider=provider,
                    shared_quota=ProviderQuotaRepository(
                        self.repository.database,
                        sha256(quota_identity.encode()).hexdigest(),
                    ),
                    request_token_reserve=max(8000, int(self.settings.get("budget.max_tokens", 8000))) + analysis_output_token_cap(self.settings),
                    priority=int(snapshot.get("priority", config["priority"])),
                    max_concurrency=(1 if options.get("daily_free_first")
                                     else config["max_concurrency"]),
                    requests_per_minute=(
                        min(int(config["rate_limit_rpm"] or 20), 20)
                        if effective_kind == "openrouter" and (
                            options.get("free_only") or str(config.get("model") or "").strip() == "openrouter/free"
                            or str(config.get("model") or "").strip().endswith(":free")
                        ) else config["rate_limit_rpm"]
                    ),
                    tokens_per_minute=config["token_limit_tpm"],
                    cooldown_seconds=config["cooldown_seconds"],
                    model=str(config.get("model") or "").strip() or None,
                    analysis_revision=self.config_revision(config),
                    daily_free_first=bool(options.get("daily_free_first")),
                )
            )
            # An explicitly selected OpenAI primary owns its billing boundary,
            # including while cooling down. Do not instantiate fallback keys.
            if len(channels) == 1 and effective_kind == "openai":
                break
        for paid in channels:
            if not paid.daily_free_first or FailoverVisionProvider._free_model(paid):
                continue
            matching_free = any(
                free.daily_free_first and FailoverVisionProvider._free_model(free)
                and free.priority < paid.priority
                and FailoverVisionProvider._same_account(free, paid)
                for free in channels
            )
            if not matching_free or float(self.settings.get("budget.daily_stop", 10)) <= 0:
                raise JobConfigurationError("CONFIG_INVALID 付費接續需要較高優先的同帳號免費 Provider 與每日付費上限")
        return FailoverVisionProvider(channels) if channels else None

    @staticmethod
    def config_revision(provider: dict[str, Any]) -> str:
        return provider_analysis_revision(provider)

    @staticmethod
    def operational_revision(provider: dict[str, Any]) -> str:
        return provider_revision(provider)

    def route_snapshot(self) -> list[dict]:
        """Allowlisted ordered routing identity for an Analysis Plan."""
        return [
            {
                "provider_id": str(row["id"]),
                "display_name": str(row.get("name") or row["id"]),
                "priority": int(row.get("priority") or 100),
                "model": str(row.get("model") or "").strip(),
                "config_revision": self.config_revision(row),
                **({"reasoning_effort": str(row.get("options", {}).get("reasoning_effort") or "none")}
                   if row.get("kind") == "openai" and row.get("options", {}).get("api_mode") == "responses" else {}),
            }
            for row in sorted(
                (row for row in self.repository.list() if row["enabled"] and row.get("usage_scope", "normal") == "normal"),
                key=lambda row: (int(row.get("priority") or 100), str(row.get("name") or row["id"])),
            )
        ]

    def usable_route_snapshot(self) -> list[dict]:
        """Return enabled, configured Vision routes without making paid/network calls."""

        analysis_model = str(self.settings.get("model.analysis_model", "gpt-4o") or "").strip()
        usable_ids: set[str] = set()
        for row in self.repository.list():
            if not (
                bool(row.get("enabled"))
                and bool(row.get("supports_vision"))
                and bool(str(row.get("base_url") or "").strip())
                and (
                    str(row.get("kind") or "").casefold() == "ollama"
                    or bool(row.get("api_key_configured"))
                )
            ):
                continue
            try:
                configured_model = str(row.get("model") or "").strip()
                # Normal analysis has no paid text-repair stage. Retired repair
                # settings must not disable an otherwise usable Vision route.
                validate_model_id(
                    str(row.get("kind") or "openai_compatible"),
                    configured_model or analysis_model,
                    base_url=str(row.get("base_url") or ""),
                    required=True,
                )
            except ValueError:
                continue
            usable_ids.add(str(row["id"]))
        return [item for item in self.route_snapshot() if str(item["provider_id"]) in usable_ids]

    def complementary_route(self, lane: str, provider_id: str | None = None) -> list[dict]:
        """An explicit single-provider lane never inherits the normal failover route."""
        key = "analysis.openai_provider_id" if lane == "openai_backfill" else "analysis.daily_openrouter_provider_id"
        provider_id = str(self.settings.get(key, "")) if provider_id is None else provider_id
        row = self.repository.get(provider_id) or {}
        expected = "openai" if lane == "openai_backfill" else "openrouter"
        kind = effective_provider_kind(str(row.get("kind") or ""), str(row.get("base_url") or "")) if row else ""
        route = next((item for item in self.usable_route_snapshot() if item["provider_id"] == provider_id), None)
        if route is None or kind != expected:
            raise ValueError(f"請先選擇已啟用且有獨立 Key 的 {expected} Vision Provider")
        if expected == "openai" and row.get("options", {}).get("api_mode") != "responses":
            raise ValueError("OpenAI 自動補齊須使用 Responses API")
        return [{**route, "complementary_lane": lane}]

    def identity_snapshot(self, provider_id: str) -> dict[str, str]:
        """Return the non-secret identity required for safe Batch cleanup.

        The account value is a one-way fingerprint of the configured secret.
        It is intentionally never returned by the Provider API/UI or written
        to logs; it only prevents a rotated credential from being mistaken for
        the account that created a remote Batch file.
        """

        config = self.repository.get(provider_id, include_secret=True)
        if config is None:
            raise ValueError(f"VLM-008 找不到 Job 指定的 Provider：{provider_id}")
        base_url = OpenAICompatibleProvider.normalize_base_url(str(config.get("base_url") or ""))
        project_id = str(config.get("options", {}).get("project_id") or config.get("project_id") or config.get("project") or "")
        account_secret = str(config.get("api_key") or "")
        account_identity = account_secret
        if config.get("options", {}).get("organization_id"):
            account_identity += "|" + str(config["options"]["organization_id"])
        return {
            "provider_config_revision": self.config_revision(config),
            "provider_base_url_fingerprint": sha256(base_url.encode("utf-8")).hexdigest(),
            "provider_project_id": project_id,
            "provider_account_fingerprint": (
                sha256(account_identity.encode("utf-8")).hexdigest() if account_secret else ""
            ),
        }
