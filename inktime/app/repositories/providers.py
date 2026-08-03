from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from inktime.app.core.json_values import json_bool
from inktime.app.core.security import mask_secret
from inktime.app.db import Database
from inktime.app.providers.config import (
    canonical_options,
    capabilities_for,
    is_openrouter_base_url,
    normalize_options,
    validate_base_url,
)
from inktime.app.repositories.settings import SecretStore


class ProviderRepository:
    def __init__(self, database: Database, secrets: SecretStore) -> None:
        self.database = database
        self.secrets = secrets

    def list(self):
        with self.database.session() as connection:
            rows = connection.execute("SELECT * FROM providers ORDER BY priority,name").fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["options"] = self._options(item)
            item["openrouter_compatible"] = (
                str(item.get("kind") or "").casefold() == "openai_compatible"
                and is_openrouter_base_url(str(item.get("base_url") or ""))
            )
            secret = self.secrets.get(row["api_key_secret"]) if row["api_key_secret"] else None
            item["api_key_masked"] = mask_secret(secret or "")
            item["pricing"] = self.pricing(str(row["id"]))
            values.append(item)
        return values

    def get(self, provider_id: str, *, include_secret: bool = False):
        with self.database.session() as connection:
            row = connection.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["options"] = self._options(item)
        item["openrouter_compatible"] = (
            str(item.get("kind") or "").casefold() == "openai_compatible"
            and is_openrouter_base_url(str(item.get("base_url") or ""))
        )
        if include_secret:
            item["api_key"] = self.secrets.get(row["api_key_secret"]) if row["api_key_secret"] else ""
        return item

    def save(self, payload: dict, user_id: str) -> str:
        provider_id = str(payload.get("id") or uuid4())
        secret_key = f"provider.{provider_id}.api_key"
        now = datetime.now(timezone.utc).isoformat()
        api_key = str(payload.get("api_key") or "")
        kind = str(payload.get("kind") or "openai_compatible").strip().lower()
        options = normalize_options(kind, payload.get("options") or {})
        base_url = validate_base_url(kind, str(payload.get("base_url") or ""), options)
        capabilities = capabilities_for(
            kind, supports_json_schema=bool(json_bool(payload, "supports_json_schema", default=True))
        )
        requested_batch = bool(json_bool(payload, "supports_batch", default=False))
        if requested_batch and not capabilities.batch:
            raise ValueError(f"PROVIDER-020 {kind} 不支援 Batch")
        if api_key:
            self.secrets.set(secret_key, api_key, user_id)
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO providers(id,name,kind,base_url,api_key_secret,enabled,priority,supports_vision,supports_batch,
                    supports_json_schema,rate_limit_rpm,token_limit_tpm,max_concurrency,timeout_seconds,cooldown_seconds,
                    options_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,base_url=excluded.base_url,
                    api_key_secret=COALESCE(providers.api_key_secret,excluded.api_key_secret),enabled=excluded.enabled,
                    priority=excluded.priority,supports_vision=excluded.supports_vision,supports_batch=excluded.supports_batch,
                    supports_json_schema=excluded.supports_json_schema,rate_limit_rpm=excluded.rate_limit_rpm,
                    token_limit_tpm=excluded.token_limit_tpm,max_concurrency=excluded.max_concurrency,
                    timeout_seconds=excluded.timeout_seconds,cooldown_seconds=excluded.cooldown_seconds,
                    options_json=excluded.options_json,updated_at=excluded.updated_at
                """,
                (
                    provider_id,
                    str(payload.get("name", "Provider")),
                    kind,
                    base_url,
                    secret_key if api_key else None,
                    int(json_bool(payload, "enabled", default=True)),
                    int(payload.get("priority", 100)),
                    int(json_bool(payload, "supports_vision", default=capabilities.vision)),
                    int(requested_batch),
                    int(capabilities.json_schema),
                    payload.get("rate_limit_rpm"),
                    payload.get("token_limit_tpm"),
                    int(payload.get("max_concurrency", 2)),
                    int(payload.get("timeout_seconds", 120)),
                    int(payload.get("cooldown_seconds", 300)),
                    canonical_options(kind, options),
                    now,
                    now,
                ),
            )
        return provider_id

    @staticmethod
    def _options(item: dict) -> dict:
        raw = item.get("options_json") or "{}"
        try:
            value = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    def pricing(self, provider_id: str) -> dict[str, dict[str, float]]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT * FROM model_pricing WHERE provider_id=? AND enabled=1", (provider_id,)
            ).fetchall()
        return {
            row["model"]: {
                "input_per_million": row["input_per_million"],
                "cached_input_per_million": row["cached_input_per_million"],
                "output_per_million": row["output_per_million"],
                "batch_multiplier": row["batch_multiplier"],
                "batch_input_per_million": row["batch_input_per_million"],
                "batch_cached_input_per_million": row["batch_cached_input_per_million"],
                "batch_output_per_million": row["batch_output_per_million"],
            }
            for row in rows
        }

    def save_pricing(self, provider_id: str, payload: dict) -> None:
        with self.database.session() as connection:
            provider = connection.execute("SELECT id FROM providers WHERE id=?", (provider_id,)).fetchone()
            if provider is None:
                raise KeyError(provider_id)
            connection.execute(
                """
                INSERT INTO model_pricing(
                    provider_id,model,input_per_million,cached_input_per_million,output_per_million,enabled,
                    batch_multiplier,batch_input_per_million,batch_cached_input_per_million,batch_output_per_million
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider_id,model) DO UPDATE SET
                    input_per_million=excluded.input_per_million,
                    cached_input_per_million=excluded.cached_input_per_million,
                    output_per_million=excluded.output_per_million,
                    enabled=excluded.enabled,
                    batch_multiplier=excluded.batch_multiplier,
                    batch_input_per_million=excluded.batch_input_per_million,
                    batch_cached_input_per_million=excluded.batch_cached_input_per_million,
                    batch_output_per_million=excluded.batch_output_per_million
                """,
                (
                    provider_id,
                    str(payload["model"]),
                    float(payload["input_per_million"]),
                    float(payload["cached_input_per_million"]),
                    float(payload["output_per_million"]),
                    int(payload.get("enabled", True)),
                    float(payload.get("batch_multiplier", 0.5)),
                    payload.get("batch_input_per_million"),
                    payload.get("batch_cached_input_per_million"),
                    payload.get("batch_output_per_million"),
                ),
            )
