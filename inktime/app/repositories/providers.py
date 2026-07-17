from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from inktime.app.core.security import mask_secret
from inktime.app.db import Database
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
            secret = self.secrets.get(row["api_key_secret"]) if row["api_key_secret"] else None
            item["api_key_masked"] = mask_secret(secret or "")
            values.append(item)
        return values

    def get(self, provider_id: str, *, include_secret: bool = False):
        with self.database.session() as connection:
            row = connection.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        if include_secret:
            item["api_key"] = self.secrets.get(row["api_key_secret"]) if row["api_key_secret"] else ""
        return item

    def save(self, payload: dict, user_id: str) -> str:
        provider_id = str(payload.get("id") or uuid4())
        secret_key = f"provider.{provider_id}.api_key"
        now = datetime.now(timezone.utc).isoformat()
        api_key = str(payload.get("api_key") or "")
        if api_key:
            self.secrets.set(secret_key, api_key, user_id)
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO providers(id,name,kind,base_url,api_key_secret,enabled,priority,supports_vision,supports_batch,
                    supports_json_schema,rate_limit_rpm,token_limit_tpm,max_concurrency,timeout_seconds,cooldown_seconds,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,base_url=excluded.base_url,
                    api_key_secret=COALESCE(providers.api_key_secret,excluded.api_key_secret),enabled=excluded.enabled,
                    priority=excluded.priority,supports_vision=excluded.supports_vision,supports_batch=excluded.supports_batch,
                    supports_json_schema=excluded.supports_json_schema,rate_limit_rpm=excluded.rate_limit_rpm,
                    token_limit_tpm=excluded.token_limit_tpm,max_concurrency=excluded.max_concurrency,
                    timeout_seconds=excluded.timeout_seconds,cooldown_seconds=excluded.cooldown_seconds,updated_at=excluded.updated_at
                """,
                (provider_id, str(payload.get("name", "Provider")), str(payload.get("kind", "openai_compatible")),
                 str(payload.get("base_url", "")), secret_key if api_key else None, int(bool(payload.get("enabled", True))),
                 int(payload.get("priority", 100)), 1, int(bool(payload.get("supports_batch", False))),
                 int(bool(payload.get("supports_json_schema", True))), payload.get("rate_limit_rpm"), payload.get("token_limit_tpm"),
                 int(payload.get("max_concurrency", 2)), int(payload.get("timeout_seconds", 120)),
                 int(payload.get("cooldown_seconds", 300)), now, now),
            )
        return provider_id

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
            }
            for row in rows
        }
