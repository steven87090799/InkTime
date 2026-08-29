from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from uuid import uuid4

from inktime.app.core.json_values import json_bool
from inktime.app.core.security import mask_secret
from inktime.app.db import Database
from inktime.app.providers.base import Usage
from inktime.app.providers.config import (
    canonical_options,
    capabilities_for,
    effective_provider_kind,
    is_openrouter_base_url,
    normalize_options,
    validate_base_url,
)
from inktime.app.providers.openai_compatible import calculate_usage_cost
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
            item["stored_kind"] = str(item.get("kind") or "")
            item["kind"] = effective_provider_kind(item["stored_kind"], str(item.get("base_url") or ""))
            item["options"] = normalize_options(item["kind"], self._options(item))
            item["openrouter_compatible"] = (
                str(item.get("stored_kind") or "").casefold() == "openai_compatible"
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
        item["stored_kind"] = str(item.get("kind") or "")
        item["kind"] = effective_provider_kind(item["stored_kind"], str(item.get("base_url") or ""))
        item["options"] = normalize_options(item["kind"], self._options(item))
        item["openrouter_compatible"] = (
            str(item.get("stored_kind") or "").casefold() == "openai_compatible"
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
        requested_kind = str(payload.get("kind") or "openai_compatible").strip().lower()
        raw_base_url = str(payload.get("base_url") or "")
        model_provided = "model" in payload
        raw_model = payload.get("model")
        if raw_model is not None and type(raw_model) is not str:
            raise ValueError("model 必須是字串")
        model = str(raw_model or "").strip()
        if (
            len(model) > 200
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in model)
        ):
            raise ValueError("model 必須是 1 至 200 字元")
        model = model or None
        kind = effective_provider_kind(requested_kind, raw_base_url)
        options = normalize_options(kind, payload.get("options") or {})
        base_url = validate_base_url(kind, raw_base_url, options)
        capabilities = capabilities_for(
            kind, supports_json_schema=bool(json_bool(payload, "supports_json_schema", default=True))
        )
        requested_batch = bool(json_bool(payload, "supports_batch", default=False))
        if requested_batch and not capabilities.batch:
            raise ValueError(f"PROVIDER-020 {kind} 不支援 Batch")
        if api_key:
            self.secrets.set(secret_key, api_key, user_id)
        with self.database.session() as connection:
            if not model_provided:
                existing = connection.execute("SELECT model FROM providers WHERE id=?", (provider_id,)).fetchone()
                if existing is not None:
                    model = existing["model"]
            connection.execute(
                """
                INSERT INTO providers(id,name,kind,base_url,model,api_key_secret,enabled,priority,supports_vision,supports_batch,
                    supports_json_schema,rate_limit_rpm,token_limit_tpm,max_concurrency,timeout_seconds,cooldown_seconds,
                    options_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,base_url=excluded.base_url,
                    model=excluded.model,
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
                    model,
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

    def save_pricing(self, provider_id: str, payload: dict) -> dict[str, int]:
        model = str(payload.get("model") or "").strip()
        if (
            not model
            or len(model) > 200
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in model)
        ):
            raise ValueError("model 必須是 1 至 200 字元")

        def number(name: str, *, nullable: bool = False, maximum: float = 1_000_000) -> float | None:
            value = payload.get(name, 0.5 if name == "batch_multiplier" else None)
            if value is None and nullable:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} 必須是有限非負數")
            result = float(value)
            if not math.isfinite(result) or result < 0 or result > maximum:
                raise ValueError(f"{name} 必須是有限非負數且不可超過 {maximum}")
            return result

        input_price = number("input_per_million")
        cached_price = number("cached_input_per_million")
        output_price = number("output_per_million")
        multiplier = number("batch_multiplier", maximum=10)
        batch_input = number("batch_input_per_million", nullable=True)
        batch_cached = number("batch_cached_input_per_million", nullable=True)
        batch_output = number("batch_output_per_million", nullable=True)
        enabled = payload.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("enabled 必須是 JSON Boolean")
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
                    model,
                    input_price,
                    cached_price,
                    output_price,
                    int(enabled),
                    multiplier,
                    batch_input,
                    batch_cached,
                    batch_output,
                ),
            )
        return self.reconcile_unknown_costs(provider_id, model)

    @staticmethod
    def _billable_unknown_sql() -> str:
        return """(
            COALESCE(input_tokens,0) > 0 OR COALESCE(output_tokens,0) > 0
            OR COALESCE(cached_tokens,0) > 0 OR COALESCE(reasoning_tokens,0) > 0
            OR COALESCE(cache_write_tokens,0) > 0 OR COALESCE(request_body_bytes,0) > 0
            OR COALESCE(image_bytes,0) > 0 OR COALESCE(actual_cost,0) > 0
            OR COALESCE(estimated_cost,0) > 0
        )"""

    def reconcile_unknown_costs(self, provider_id: str, model: str) -> dict[str, int]:
        """Reprice only billable unknown rows after a complete model price is saved."""

        provider_key = str(provider_id)
        model_key = str(model)
        pricing = self.pricing(provider_key).get(model_key)
        evidence = self._billable_unknown_sql()
        reconciled = 0
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT id,input_tokens,output_tokens,cached_tokens,reasoning_tokens,cache_write_tokens,
                       processing_mode
                FROM api_usage
                WHERE provider_id=? AND model=? AND cost_source='unknown' AND {evidence}
                ORDER BY id
                """,
                (provider_key, model_key),
            ).fetchall()
            updates = []
            for row in rows:
                if pricing is None:
                    continue
                estimated = calculate_usage_cost(
                    pricing,
                    Usage(
                        input_tokens=int(row["input_tokens"] or 0),
                        output_tokens=int(row["output_tokens"] or 0),
                        cached_tokens=int(row["cached_tokens"] or 0),
                        reasoning_tokens=int(row["reasoning_tokens"] or 0),
                        cache_write_tokens=int(row["cache_write_tokens"] or 0),
                    ),
                    batch=str(row["processing_mode"] or "") == "batch",
                )
                if estimated is not None:
                    updates.append((float(estimated), int(row["id"])))
            if updates:
                connection.executemany(
                    "UPDATE api_usage SET estimated_cost=?,actual_cost=NULL,cost_source='estimated' WHERE id=?",
                    updates,
                )
                reconciled = len(updates)
            remaining = connection.execute(
                f"""
                SELECT COUNT(*) FROM api_usage
                WHERE provider_id=? AND model=? AND cost_source='unknown' AND {evidence}
                """,
                (provider_key, model_key),
            ).fetchone()
        return {"reconciled_count": reconciled, "remaining_unknown_count": int(remaining[0] or 0)}
