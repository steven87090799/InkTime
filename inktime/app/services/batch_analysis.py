"""Persistent, restart-safe OpenAI Batch analysis lifecycle.

The service deliberately keeps remote state in SQLite and treats every output
line as an independently auditable item.  It never waits on a remote Batch in
the worker that submitted it.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import resource
import sqlite3
import time
from typing import Any, Callable, Iterable, Literal, Mapping
from uuid import uuid4

from inktime.app.core.paths import safe_join
from inktime.app.core.logging import log_event
from inktime.app.domain.analysis import (
    AnalysisValidationError,
    canonical_json,
    fingerprint,
    normalize_reasoning_effort,
    validate_analysis_result,
)
from inktime.app.domain.analysis.plan import SCHEMA_VERSION, provider_prompt_contract_sha256
from inktime.app.domain.analysis.schema import validate_model_response
from inktime.app.domain.photos.quality_policy import is_confirmed_screenshot
from inktime.app.services.analysis import CAPTION_VARIANTS_TOKEN_CAP, FULL_ANALYSIS_TOKEN_CAP
from inktime.app.providers.base import Usage
from inktime.app.providers.openai_compatible import calculate_usage_cost
from inktime.app.repositories.analysis_reservations import (
    AnalysisReservationConflict,
    reserved_analysis_sql,
)
from inktime.app.repositories.analysis_batches import (
    ACTIVE_BATCH_STATUSES,
    TERMINAL_BATCH_STATUSES,
    AnalysisBatchRepository,
    utc_now,
)


MAX_OPENAI_REQUESTS = 50_000
MAX_OPENAI_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_ITEMS = 500
DEFAULT_MAX_BYTES = 150 * 1024 * 1024
CUSTOM_ID_RE = re.compile(r"^ibt:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REMOTE_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
LOGGER = logging.getLogger("batch_analysis")


class BatchLifecycleError(RuntimeError):
    def __init__(self, message: str, code: str = "BATCH-001") -> None:
        super().__init__(message)
        self.code = code


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)
    # Linux reports KiB; macOS reports bytes.
    return value * 1024 if value < 10_000_000 else value


def stream_jsonl_shards(
    root: Path,
    items: Iterable[dict[str, Any]],
    line_factory: Callable[[dict[str, Any]], bytes],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    path_prefix: str = "input",
) -> list[dict[str, Any]]:
    """Write JSONL one item at a time and split by actual bytes.

    Only shard metadata is retained in memory.  The request body, encoded
    image, JSON line, and source thumbnail can therefore be released after one
    item is written.
    """

    max_items = max(1, min(int(max_items), MAX_OPENAI_REQUESTS))
    max_bytes = max(1, min(int(max_bytes), MAX_OPENAI_BYTES))
    root = root.resolve()
    shards: list[dict[str, Any]] = []
    handle = None
    current: dict[str, Any] | None = None

    def close_current() -> None:
        nonlocal handle, current
        if handle is None or current is None:
            return
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(Path(current["path"]), 0o600)
        current["peak_rss_bytes"] = _peak_rss_bytes()
        shards.append(current)
        handle = None
        current = None

    try:
        for item in items:
            line = line_factory(item)
            if not isinstance(line, bytes):
                raise TypeError("Batch JSONL line_factory 必須回傳 bytes")
            if not line.endswith(b"\n"):
                line += b"\n"
            line_bytes = len(line)
            if line_bytes > max_bytes:
                raise BatchLifecycleError(
                    f"單一 Batch Request 超過安全 JSONL 分片上限：{line_bytes} bytes",
                    "BATCH-INPUT-TOO-LARGE",
                )
            if (
                current is None
                or int(current["items_count"]) >= max_items
                or int(current["bytes"]) + line_bytes > max_bytes
            ):
                close_current()
                shard_number = len(shards) + 1
                directory = root / f"{shard_number:04d}"
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{path_prefix}.jsonl"
                handle = path.open("wb")
                os.chmod(path, 0o600)
                current = {
                    "path": str(path),
                    "items": [],
                    "items_count": 0,
                    "bytes": 0,
                    "peak_rss_bytes": 0,
                }
            assert handle is not None and current is not None
            handle.write(line)
            current["items"].append(str(item["id"]))
            current["items_count"] = int(current["items_count"]) + 1
            current["bytes"] = int(current["bytes"]) + line_bytes
            del line
        close_current()
    except Exception:
        if handle is not None:
            handle.close()
        # A preparation failure can happen before the Batch row has a
        # local_input_path.  Remove only the exact generated Batch directory
        # so a partial JSONL can never survive as an untracked upload candidate.
        if root.exists() and root.is_dir():
            for candidate in sorted(root.rglob("*"), key=lambda value: len(value.parts), reverse=True):
                try:
                    if candidate.is_file() or candidate.is_symlink():
                        candidate.unlink()
                    elif candidate.is_dir():
                        candidate.rmdir()
                except OSError:
                    pass
        raise
    return shards


class BatchAnalysisService:
    ENDPOINT = "/v1/chat/completions"

    def __init__(
        self,
        database,
        batches: AnalysisBatchRepository,
        jobs,
        job_service,
        photos,
        providers,
        provider_service,
        usage,
        thumbnails,
        analysis,
        settings,
        data_dir: Path,
        observability=None,
        scoring_repository=None,
    ) -> None:
        self.database = database
        self.batches = batches
        self.jobs = jobs
        self.job_service = job_service
        self.photos = photos
        self.providers = providers
        self.provider_service = provider_service
        self.usage = usage
        self.thumbnails = thumbnails
        self.analysis = analysis
        self.settings = settings
        self.data_dir = data_dir.resolve()
        self.batch_root = self.data_dir / "batches"
        self.batch_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.batch_root, 0o700)
        self.observability = observability
        self.scoring_repository = scoring_repository

    def _activity(self, severity: str, event: str, message: str, **fields: Any) -> None:
        safe_fields = {
            key: fields[key]
            for key in ("batch_id", "batch_item_id", "job_id", "count", "status", "error_code")
            if key in fields
        }
        level = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "CRITICAL": logging.CRITICAL,
        }.get(str(severity).upper(), logging.INFO)
        log_event(
            LOGGER,
            level,
            message,
            event=event,
            batch_id=str(fields.get("batch_id") or ""),
            batch_item_id=str(fields.get("batch_item_id") or ""),
            job_id=str(fields.get("job_id") or ""),
            error_code=str(fields.get("error_code") or ""),
            details=safe_fields,
        )
        if self.observability is not None:
            self.observability.record(severity, "analysis_batch", event, message, **safe_fields)

    def _batch_limits(self) -> tuple[int, int]:
        return (
            int(self.settings.get("batch.max_items_per_shard", DEFAULT_MAX_ITEMS)),
            int(self.settings.get("batch.max_jsonl_bytes", DEFAULT_MAX_BYTES)),
        )

    def _side_effect_lease_until(self, provider_id: str) -> str:
        """Give each external request a fresh lease longer than its timeout."""

        try:
            provider = self.providers.get(provider_id)
            timeout = int((provider or {}).get("timeout_seconds") or 120)
        except Exception:
            timeout = 120
        seconds = max(900, timeout * 3 + 120)
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

    def _provider_route(self) -> list[dict[str, Any]]:
        usable_ids = {
            str(item["provider_id"]) for item in self.provider_service.usable_route_snapshot()
        }
        rows = [
            row
            for row in self.providers.list()
            if bool(row.get("enabled"))
            and bool(row.get("supports_batch"))
            and str(row.get("kind") or "").lower() != "openrouter"
            and str(row.get("id")) in usable_ids
        ]
        rows.sort(key=lambda row: (int(row.get("priority") or 100), str(row.get("name") or row["id"])))
        if not rows:
            raise BatchLifecycleError("沒有已啟用且支援 Batch 的 Provider", "BATCH-PROVIDER-001")
        row = rows[0]
        snapshot = self.provider_service.usable_route_snapshot()
        selected = next((item for item in snapshot if str(item["provider_id"]) == str(row["id"])), None)
        if selected is None:
            raise BatchLifecycleError("Batch Provider 不在目前的 Frozen Route", "BATCH-PROVIDER-002")
        return [selected]

    def _plan(self, route: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], str, str]:
        route = route or self._provider_route()
        # The scoring repository is intentionally read through the app graph by
        # bootstrap; this fallback keeps direct service tests dependency-light.
        scoring = getattr(self, "scoring", None) or {
            "id": "",
            "memory_weight": 25,
            "visual_weight": 25,
            "local_weight": 25,
            "favorite_bonus": 0,
        }
        scoring_repository = getattr(self, "scoring_repository", None)
        if scoring_repository is not None:
            scoring = dict(scoring_repository.current())
        plan = self.analysis.build_plan(
            strategy="single",
            provider_route=route,
            scoring_profile=scoring,
        )
        model = str(self.settings.get("batch.model", "gpt-5.6-luna")).strip()
        if not model:
            raise BatchLifecycleError("Batch 模型不可空白", "BATCH-MODEL-001")
        # Batch is a single full analysis.  The legacy smart_two_stage path is
        # not used and no second model call is made during import.
        plan["strategy"] = "single"
        plan["model"] = model
        plan["processing_mode"] = "batch"
        plan["batch_endpoint"] = self.ENDPOINT
        plan["batch_schema_kind"] = "full"
        plan["batch_completion_window"] = "24h"
        plan["reasoning_effort"] = normalize_reasoning_effort(
            self.settings.get("batch.reasoning_effort", "none")
        )
        plan["provider_prompt_contract_sha256"] = provider_prompt_contract_sha256(
            prompt_version=str(plan.get("prompt_version") or ""),
            scoring_rules_sha256=str(plan.get("scoring_rules_sha256") or ""),
            schema_version=int(plan.get("schema_version", SCHEMA_VERSION)),
            schema_kind=str(plan.get("schema_kind") or "full"),
            caption_generation_controls=dict(plan.get("caption_controls") or {}),
            reasoning_effort=str(plan["reasoning_effort"]),
            provider_behavior_revision=str(plan.get("provider_behavior_revision") or ""),
        )
        return plan, fingerprint(plan), model

    def _base_candidate_sql(self) -> tuple[str, list[Any]]:
        active = sorted(ACTIVE_BATCH_STATUSES)
        marks = ",".join("?" for _ in active)
        return (
            f"""
            SELECT p.*,l.root_path
            FROM photos p JOIN libraries l ON l.id=p.library_id
            WHERE p.lifecycle_status='active' AND p.eligible=1
              AND NOT {reserved_analysis_sql()}
              AND COALESCE(p.never_upload,0)=0
              AND COALESCE(p.exclusion_status,'')!='manually_excluded'
              AND COALESCE(p.sha256,'')!=''
              AND p.local_features_status='complete'
              AND NOT EXISTS (
                  SELECT 1 FROM photo_analysis a
                  WHERE a.photo_id=p.id AND a.analysis_fingerprint=? AND COALESCE(a.schema_kind,'basic')='full'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM analysis_batch_items i
                  JOIN analysis_batches b ON b.id=i.batch_id
                  WHERE i.photo_id=p.id AND b.status IN ({marks})
              )
            ORDER BY COALESCE(p.local_candidate_score,-1) DESC,
                     COALESCE(p.captured_at,p.created_at),p.id
            """,  # noqa: S608 -- only the placeholder count is dynamic.
            active,
        )

    def _candidate_rows(
        self,
        *,
        scope: str,
        analysis_fingerprint: str,
        provider_id: str,
        model: str,
        plan: dict[str, Any],
        photo_ids: Iterable[str] | None,
        sample_count: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int], str | None]:
        query, active_parameters = self._base_candidate_sql()
        parameters: list[Any] = [analysis_fingerprint, *active_parameters]
        if scope == "manual_selection":
            requested = list(dict.fromkeys(str(value) for value in (photo_ids or [])))
            if not requested:
                return [], {"never_upload_excluded": 0, "confirmed_screenshot_excluded": 0, "cache_hits": 0, "sha_duplicates": 0}, None
            marks = ",".join("?" for _ in requested)
            query = query.replace(
                "ORDER BY COALESCE(p.local_candidate_score,-1) DESC,\n                     COALESCE(p.captured_at,p.created_at),p.id",
                f"AND p.id IN ({marks})\n            ORDER BY COALESCE(p.local_candidate_score,-1) DESC,\n                     COALESCE(p.captured_at,p.created_at),p.id",
            )
            parameters.extend(requested)
        with self.database.session() as connection:
            rows = [dict(row) for row in connection.execute(query, parameters).fetchall()]
            never_upload = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM photos p
                    WHERE p.lifecycle_status='active' AND p.eligible=1 AND COALESCE(p.never_upload,0)=1
                    """
                ).fetchone()[0]
            )
        safe_rows: list[dict[str, Any]] = []
        confirmed_screenshot_excluded = 0
        prefilter = dict(plan.get("prefilter") or {})
        policy_settings = {
            "analysis.prefilter_enabled": bool(prefilter.get("enabled", True)),
            "analysis.prefilter_screenshots": bool(prefilter.get("screenshots_enabled", True)),
            "analysis.prefilter_low_quality": bool(prefilter.get("low_quality_enabled", True)),
            "analysis.prefilter_sensitivity": str(prefilter.get("sensitivity", "conservative")),
            "analysis.e6_prefilter_enabled": bool(prefilter.get("e6_enabled", True)),
            "analysis.e6_min_score": float(prefilter.get("e6_min_score", 25)),
        }
        for row in rows:
            if is_confirmed_screenshot(row):
                confirmed_screenshot_excluded += 1
                continue
            if self.analysis.prefilter_snapshot(row, policy_settings=policy_settings)["excluded"]:
                continue
            try:
                source = safe_join(Path(str(row["root_path"])), str(row["relative_path"]))
            except (OSError, ValueError):
                continue
            if not source.is_file():
                continue
            safe_rows.append({"photo": row, "source": source})
        seed: str | None = None
        if scope == "sample":
            seed = hashlib.sha256(f"{scope}:{analysis_fingerprint}".encode("utf-8")).hexdigest()
            safe_rows.sort(
                key=lambda item: hashlib.sha256(f"{seed}:{item['photo']['id']}".encode("utf-8")).hexdigest()
            )
            safe_rows = safe_rows[: max(1, min(int(sample_count), 100_000))]
        candidates: list[dict[str, Any]] = []
        seen_sha: set[str] = set()
        cache_hits = 0
        sha_duplicates = 0
        vision_input = dict(plan.get("vision_input") or plan.get("high_vision_input") or {})
        vision_input["schema_kind"] = "full"
        vision_input["reasoning_effort"] = str(plan["reasoning_effort"])
        for item in safe_rows:
            photo = item["photo"]
            content_sha = str(photo["sha256"] or "").casefold()
            if content_sha in seen_sha:
                sha_duplicates += 1
                continue
            seen_sha.add(content_sha)
            vision_fp = fingerprint(
                {
                    "content_sha256": content_sha,
                    "actual_provider": provider_id,
                    "model": model,
                    "prompt_version": str(plan["prompt_version"]),
                    "schema_version": SCHEMA_VERSION,
                    "schema_kind": "full",
                    "reasoning_effort": str(plan["reasoning_effort"]),
                    "provider_prompt_contract_sha256": str(plan.get("provider_prompt_contract_sha256") or ""),
                    **vision_input,
                }
            )
            cached = self.photos.get_ai_cache(
                content_sha256=content_sha,
                provider=provider_id,
                model_name=model,
                prompt_version=str(plan["prompt_version"]),
                schema_version=SCHEMA_VERSION,
                schema_kind="full",
                vision_request_fingerprint=vision_fp,
            )
            if cached is not None:
                try:
                    validate_analysis_result(cached["result"])
                except (AnalysisValidationError, TypeError, ValueError):
                    pass
                else:
                    cache_hits += 1
                    continue
            candidates.append(
                {
                    "photo": photo,
                    "source": item["source"],
                    "content_sha256": content_sha,
                    "vision_request_fingerprint": vision_fp,
                    "vision_input_spec_json": canonical_json(vision_input),
                }
            )
        return (
            candidates,
            {
                "never_upload_excluded": never_upload,
                "confirmed_screenshot_excluded": confirmed_screenshot_excluded,
                "cache_hits": cache_hits,
                "sha_duplicates": sha_duplicates,
            },
            seed,
        )

    def estimate(
        self,
        *,
        scope: str = "sample",
        sample_count: int = 100,
        photo_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if scope not in {"sample", "all_eligible_missing_analysis", "new_or_changed", "manual_selection"}:
            raise BatchLifecycleError("不支援的 Batch scope", "BATCH-SCOPE-001")
        plan, analysis_fp, model = self._plan()
        route = plan["provider_route"]
        provider_id = str(route[0]["provider_id"])
        candidates, skipped, seed = self._candidate_rows(
            scope=scope,
            analysis_fingerprint=analysis_fp,
            provider_id=provider_id,
            model=model,
            plan=plan,
            photo_ids=photo_ids,
            sample_count=sample_count,
        )
        return self._estimate_candidates(
            scope=scope,
            sample_seed=seed,
            candidates=candidates,
            skipped=skipped,
            model=model,
            analysis_fingerprint=analysis_fp,
            provider_id=provider_id,
        )

    def _estimate_candidates(
        self,
        *,
        scope: str,
        sample_seed: str | None,
        candidates: list[dict[str, Any]],
        skipped: dict[str, int],
        model: str,
        analysis_fingerprint: str,
        provider_id: str,
    ) -> dict[str, Any]:
        max_items, max_bytes = self._batch_limits()
        # This is deliberately an estimate; the submit path records actual
        # shard bytes after ThumbnailCache and JSON serialization.
        estimated_input_tokens = len(candidates) * int(
            self.settings.get("batch.estimated_input_tokens", 2500)
        )
        estimated_output_tokens = len(candidates) * int(
            self.settings.get("batch.estimated_output_tokens", 500)
        )
        pricing = self.providers.pricing(provider_id).get(model)
        estimated_cost = calculate_usage_cost(
            pricing,
            Usage(input_tokens=estimated_input_tokens, output_tokens=estimated_output_tokens),
            batch=True,
        )
        return {
            "scope": scope,
            "sample_seed": sample_seed,
            "candidate_count": len(candidates),
            "cache_hits": skipped["cache_hits"],
            "sha_duplicates": skipped["sha_duplicates"],
            "never_upload_excluded": skipped["never_upload_excluded"],
            "submitted_count": len(candidates),
            "estimated_jsonl_bytes": min(MAX_OPENAI_BYTES, len(candidates) * 350_000),
            "estimated_shard_count": max(0, (len(candidates) + max_items - 1) // max_items),
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "estimated_cost": round(estimated_cost, 6) if estimated_cost is not None else None,
            "cost_source": "estimated" if estimated_cost is not None else "unknown",
            "cost_complete": estimated_cost is not None,
            "model": model,
            "analysis_fingerprint": analysis_fingerprint,
            "max_items_per_shard": max_items,
            "max_jsonl_bytes": max_bytes,
        }

    def _provider(self, provider_id: str, plan: dict[str, Any]):
        configured = self.providers.get(provider_id)
        if configured is not None and str(configured.get("kind") or "").lower() == "openrouter":
            raise BatchLifecycleError("OpenRouter 僅支援即時 Chat Completions，不可進入 Batch", "BATCH-OPENROUTER-001")
        route = [item for item in plan["provider_route"] if str(item["provider_id"]) == provider_id]
        if not route:
            raise BatchLifecycleError("Frozen Batch Provider 不存在", "BATCH-PROVIDER-003")
        provider = self.provider_service.build_router(
            route,
            scoring_rules=str(plan.get("scoring_rules") or ""),
            caption_controls=plan.get("caption_controls") or None,
        )
        if provider is None:
            raise BatchLifecycleError("Frozen Batch Provider 無法建立", "BATCH-PROVIDER-004")
        return provider

    def _provider_identity(self, provider_id: str) -> dict[str, str]:
        snapshot = getattr(self.provider_service, "identity_snapshot", None)
        if callable(snapshot):
            identity = snapshot(provider_id)
            return {
                "provider_config_revision": str(identity.get("provider_config_revision") or ""),
                "provider_base_url_fingerprint": str(identity.get("provider_base_url_fingerprint") or ""),
                "provider_project_id": str(identity.get("provider_project_id") or ""),
                "provider_account_fingerprint": str(identity.get("provider_account_fingerprint") or ""),
            }
        # Compatibility for minimal third-party test services.  Production
        # ProviderService always supplies the complete non-secret identity.
        route = next(
            (
                item
                for item in self.provider_service.route_snapshot()
                if str(item.get("provider_id")) == provider_id
            ),
            None,
        )
        if route is None:
            raise BatchLifecycleError("Frozen Provider identity 不存在", "BATCH-PROVIDER-IDENTITY-001")
        return {
            "provider_config_revision": str(route.get("config_revision") or ""),
            "provider_base_url_fingerprint": str(route.get("base_url_fingerprint") or ""),
            "provider_project_id": str(route.get("project_id") or ""),
            "provider_account_fingerprint": str(route.get("account_fingerprint") or ""),
        }

    def _cleanup_identity_matches(self, batch: dict[str, Any]) -> tuple[bool, str, str]:
        """Fail closed unless cleanup still targets the original account."""

        required = (
            "provider_config_revision",
            "provider_base_url_fingerprint",
            "provider_project_id",
            "provider_account_fingerprint",
        )
        if any(batch.get(key) is None for key in required):
            return False, "BATCH-CLEANUP-PROVIDER-LEGACY", "Batch 缺少持久化 Provider identity，必須人工確認"
        try:
            current = self._provider_identity(str(batch["provider_id"]))
        except Exception as exc:
            return False, "BATCH-CLEANUP-PROVIDER-UNKNOWN", str(exc)
        mismatches = [key for key in required if str(batch.get(key) or "") != str(current.get(key) or "")]
        if mismatches:
            return (
                False,
                "BATCH-CLEANUP-PROVIDER-MISMATCH",
                f"Provider identity 不一致：{','.join(mismatches)}",
            )
        return True, "", ""

    def _mark_cleanup_provider_unknown(self, batch_id: str, code: str, message: str) -> None:
        current = self.batches.get(batch_id)
        if current is None:
            return
        changes: dict[str, Any] = {
            "cleanup_status": "partial",
            "cleanup_error_code": code,
            "cleanup_error_message": message[:1000],
        }
        if str(current["status"]) not in TERMINAL_BATCH_STATUSES:
            changes["status"] = "cleanup_pending"
        self.batches.update_batch(batch_id, **changes)

    def _hold_terminal_cleanup_for_invalid_plan(
        self,
        batch: Mapping[str, Any],
        *,
        error_code: str,
        error_message: str,
        cleanup_status: str = "partial",
    ) -> dict[str, Any]:
        """Record a cleanup hold without changing terminal Batch semantics."""

        if str(batch["status"]) not in TERMINAL_BATCH_STATUSES:
            raise ValueError("terminal cleanup hold 只能套用於 terminal Batch")
        self.batches.update_batch(
            str(batch["id"]),
            cleanup_status=cleanup_status,
            cleanup_error_code=error_code,
            cleanup_error_message=error_message[:1000],
        )
        current = self.batches.get(str(batch["id"]))
        return dict(current) if current is not None else dict(batch)

    @staticmethod
    def _cleanup_action_for(batch: dict[str, Any]) -> str:
        action = str(batch.get("cleanup_final_action") or "none")
        if action != "none":
            return action
        status = str(batch.get("status") or "")
        remote_status = str(batch.get("remote_status") or "")
        if status in {"cancelled", "cancelling"} or remote_status == "cancelled":
            return "cancel"
        if remote_status == "abandoned":
            return "abandon"
        return "complete"

    def _cleanup_plan(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Build a cleanup-only route after immutable identity verification."""

        matched, code, message = self._cleanup_identity_matches(batch)
        if not matched:
            raise BatchLifecycleError(message, code)

        route = [
            item
            for item in self.provider_service.route_snapshot()
            if str(item.get("provider_id")) == str(batch["provider_id"])
        ]
        if not route:
            raise BatchLifecycleError(
                "原 Provider route 已不存在，無法安全清理遠端檔案", "BATCH-CLEANUP-PROVIDER-001"
            )
        return {"provider_route": route, "scoring_rules": ""}

    def _retrieve_and_validate_remote_file(
        self, batch: dict[str, Any], file_id: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Read ownership metadata.  This phase never issues DELETE."""

        matched, code, message = self._cleanup_identity_matches(batch)
        if not matched:
            raise BatchLifecycleError(message, code)
        route = next(
            (
                item
                for item in plan.get("provider_route", [])
                if str(item.get("provider_id")) == str(batch["provider_id"])
            ),
            None,
        )
        if not route or str(route.get("config_revision") or "") != str(
            batch.get("provider_config_revision") or ""
        ):
            raise BatchLifecycleError(
                "Frozen Plan Provider revision 與 Batch identity 不一致",
                "BATCH-ABANDON-FILE-008",
            )
        provider = None
        try:
            provider = self._provider(str(batch["provider_id"]), plan)
            retrieve_file = getattr(provider, "retrieve_file", None)
            if not callable(retrieve_file):
                raise BatchLifecycleError(
                    "Provider 不支援遠端 File metadata Recovery", "BATCH-ABANDON-FILE-003"
                )
            remote = retrieve_file(file_id)
        finally:
            if provider is not None and callable(getattr(provider, "close", None)):
                provider.close()
        if not isinstance(remote, dict) or str(remote.get("id") or "") != file_id:
            raise BatchLifecycleError("遠端 File ID 不一致", "BATCH-ABANDON-FILE-004")
        if str(remote.get("purpose") or "") != "batch":
            raise BatchLifecycleError("遠端 File purpose 不是 batch", "BATCH-ABANDON-FILE-005")
        if str(remote.get("filename") or "") != f"inktime-batch-{batch['id']}.jsonl":
            raise BatchLifecycleError("遠端 File filename 不一致", "BATCH-ABANDON-FILE-006")
        if remote.get("bytes") is not None and batch.get("input_file_bytes") is not None:
            if int(remote.get("bytes") or 0) != int(batch["input_file_bytes"]):
                raise BatchLifecycleError("遠端 File bytes 不一致", "BATCH-ABANDON-FILE-009")
        remote_provider = remote.get("provider_id") or remote.get("provider")
        if remote_provider is not None and str(remote_provider) != str(batch["provider_id"]):
            raise BatchLifecycleError("遠端 File Provider context 不一致", "BATCH-ABANDON-FILE-008")
        remote_project = remote.get("project_id") or remote.get("project")
        if remote_project is not None and str(remote_project) != str(batch.get("provider_project_id") or ""):
            raise BatchLifecycleError("遠端 File project context 不一致", "BATCH-ABANDON-FILE-008")
        remote_account = remote.get("account_fingerprint")
        if remote_account is not None and str(remote_account) != str(
            batch.get("provider_account_fingerprint") or ""
        ):
            raise BatchLifecycleError("遠端 File account context 不一致", "BATCH-ABANDON-FILE-008")
        return remote

    def _delete_verified_remote_file(self, batch: dict[str, Any], file_id: str, plan: dict[str, Any]) -> None:
        """Delete only after a separate successful ownership-validation phase."""

        matched, code, message = self._cleanup_identity_matches(batch)
        if not matched:
            raise BatchLifecycleError(message, code)
        provider = None
        try:
            provider = self._provider(str(batch["provider_id"]), plan)
            provider.delete_remote_file(file_id)
        except Exception as exc:
            status = int(getattr(exc, "http_status", 0) or 0)
            error_code = str(getattr(exc, "code", ""))
            if status not in {404, 410} and not any(
                marker in error_code.casefold() for marker in ("not_found", "not-found", "expired")
            ):
                raise
        finally:
            if provider is not None and callable(getattr(provider, "close", None)):
                provider.close()

    def _line_factory(self, provider, plan: dict[str, Any]) -> Callable[[dict[str, Any]], bytes]:
        global_cap = int(self.settings.get("budget.max_tokens", 8000))
        requested_cap = int(
            self.settings.get(
                "budget.caption_variants_max_tokens"
                if (plan.get("caption_controls") or {}).get("caption_variants_enabled")
                else "budget.full_analysis_max_tokens",
                3072 if (plan.get("caption_controls") or {}).get("caption_variants_enabled") else 2048,
            )
        )
        hard_cap = (
            CAPTION_VARIANTS_TOKEN_CAP
            if (plan.get("caption_controls") or {}).get("caption_variants_enabled")
            else FULL_ANALYSIS_TOKEN_CAP
        )
        max_tokens = max(256, min(global_cap, requested_cap, hard_cap))

        def make_line(item: dict[str, Any]) -> bytes:
            body = provider.build_analysis_request_body(
                image_path=item["thumbnail"],
                model=str(plan.get("model") or plan.get("high_model") or ""),
                detail=str((plan.get("vision_input") or plan.get("high_vision_input") or {})["detail"]),
                stage="single",
                max_tokens=max_tokens,
                caption_controls=plan.get("caption_controls") or None,
                reasoning_effort=str(plan["reasoning_effort"]),
            )
            payload = {
                "custom_id": str(item["custom_id"]),
                "method": "POST",
                "url": self.ENDPOINT,
                "body": body,
            }
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

        return make_line

    def _prepare_shards(
        self, batch_id: str, candidates: list[dict[str, Any]], plan: dict[str, Any], provider
    ) -> list[str]:
        items = self.batches.items(batch_id)
        by_photo = {str(item["photo_id"]): item for item in items}
        stream_items: list[dict[str, Any]] = []
        for candidate in candidates:
            db_item = by_photo[str(candidate["photo"]["id"])]
            stream_items.append({**candidate, **db_item})

        def factory(item: dict[str, Any]) -> bytes:
            source = Path(str(item["source"]))
            with self.thumbnails.acquire_for_use(
                source,
                str(item["content_sha256"]),
                int((plan.get("vision_input") or plan.get("high_vision_input") or {})["max_side"]),
            ) as thumbnail:
                item["thumbnail"] = thumbnail
                try:
                    return self._line_factory(provider, plan)(item)
                finally:
                    item.pop("thumbnail", None)

        max_items, max_bytes = self._batch_limits()
        shards = stream_jsonl_shards(
            self.batch_root / batch_id,
            stream_items,
            factory,
            max_items=max_items,
            max_bytes=max_bytes,
        )
        batch_ids: list[str] = []
        for index, shard in enumerate(shards):
            current_id = batch_id if index == 0 else str(uuid4())
            path = Path(str(shard["path"]))
            if index > 0:
                child_directory = self.batch_root / current_id / path.parent.name
                child_directory.parent.mkdir(parents=True, exist_ok=True)
                self.batches.create_child_batch(
                    batch_id,
                    current_id,
                    shard["items"],
                    local_input_path=str(child_directory / path.name),
                    total_items=int(shard["items_count"]),
                    peak_rss_bytes=int(shard["peak_rss_bytes"]),
                    input_file_bytes=int(shard["bytes"]),
                )
                # Persist the child reservation before moving the local shard.
                # If the process dies between these two local operations, the
                # parent remains a preparing sibling and startup cleanup can
                # remove the original shard deterministically.
                os.replace(path.parent, child_directory)
                path = child_directory / path.name
            else:
                self.batches.update_batch(
                    batch_id,
                    local_input_path=str(path),
                    total_items=int(shard["items_count"]),
                    peak_rss_bytes=int(shard["peak_rss_bytes"]),
                    input_file_bytes=int(shard["bytes"]),
                )
            batch_ids.append(current_id)
        return batch_ids

    def _submit_one(self, batch_id: str, plan: dict[str, Any]) -> None:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if str(batch["status"]) in {"upload_unknown", "submission_unknown"}:
            raise BatchLifecycleError(
                "Batch 外部 side effect 結果未知，必須先由管理員 Recovery 或 Abandon",
                "BATCH-UNKNOWN-HOLD",
            )
        provider = None
        owner = f"submit:{uuid4()}"
        upload_attempt: str | None = None
        submission_attempt: str | None = None
        remote_created = False
        try:
            current = self.batches.get(batch_id)
            if current is None:
                raise KeyError(batch_id)
            if not current["input_file_id"]:
                upload_attempt = str(uuid4())
                upload_lease_until = self._side_effect_lease_until(str(current["provider_id"]))
                if not self.batches.claim_upload(batch_id, owner, upload_attempt, upload_lease_until):
                    raise BatchLifecycleError("Batch upload 已由其他執行者持有", "BATCH-ALREADY-CLAIMED")
                current = self.batches.get(batch_id)
                if current is None:
                    raise KeyError(batch_id)
                input_path = Path(str(current["local_input_path"] or ""))
                if not input_path.is_file():
                    raise BatchLifecycleError("找不到待上傳的 JSONL 分片", "BATCH-INPUT-002")
                input_file_bytes = input_path.stat().st_size
                provider = self._provider(str(current["provider_id"]), plan)
                upload_method = provider.upload_batch_file
                parameters = inspect.signature(upload_method).parameters
                if "remote_filename" in parameters or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
                ):
                    input_file_id = upload_method(
                        input_path, remote_filename=f"inktime-batch-{batch_id}.jsonl"
                    )
                else:  # compatibility with older test and third-party providers
                    input_file_id = upload_method(input_path)
                if not isinstance(input_file_id, str) or not input_file_id:
                    raise BatchLifecycleError("上傳回應缺少 file id", "BATCH-UPLOAD-UNKNOWN")
                if not self.batches.complete_upload(
                    batch_id,
                    upload_attempt,
                    owner,
                    input_file_id,
                    input_file_bytes=input_file_bytes,
                ):
                    raise BatchLifecycleError("Upload 回應已失去本機 claim", "BATCH-CLAIM-STALE")
            current = self.batches.get(batch_id)
            if current is None or not current["input_file_id"]:
                raise BatchLifecycleError("Batch 缺少已保存的 input_file_id", "BATCH-UPLOAD-UNKNOWN")
            submission_attempt = str(uuid4())
            # Do not inherit an upload lease: a slow upload can consume most
            # of it, which would let a second scheduler race the POST /batches.
            submission_lease_until = self._side_effect_lease_until(str(current["provider_id"]))
            if not self.batches.claim_submission(batch_id, owner, submission_attempt, submission_lease_until):
                raise BatchLifecycleError("Batch submission 已由其他執行者持有", "BATCH-ALREADY-CLAIMED")
            if provider is None:
                provider = self._provider(str(current["provider_id"]), plan)
            remote = provider.create_batch(
                str(current["input_file_id"]),
                completion_window="24h",
                metadata={"inktime_batch_id": batch_id, "inktime_version": "batch-lifecycle-v1"},
                output_expires_after_seconds=int(
                    self.settings.get("batch.output_expires_after_seconds", 86400)
                ),
            )
            remote_created = True
            remote_id = remote.get("id")
            if not isinstance(remote_id, str) or not remote_id:
                raise BatchLifecycleError("遠端 Batch 建立回應缺少 id", "BATCH-SUBMISSION-UNKNOWN")
            state = self.batches.complete_submission(batch_id, submission_attempt, owner, remote)
            if state is None:
                raise BatchLifecycleError("Batch submission 回應已失去本機 claim", "BATCH-CLAIM-STALE")
            self._activity("INFO", "batch_submitted", "Batch 已提交", batch_id=batch_id, status="validating")
        except Exception as exc:
            current = self.batches.get(batch_id)
            has_uploaded_file = bool(current is not None and current["input_file_id"])
            code = str(getattr(exc, "code", "BATCH-SUBMIT-001"))
            ambiguous = bool(getattr(exc, "ambiguous", False)) or code in {
                "BATCH-SUBMISSION-UNKNOWN",
                "BATCH-UPLOAD-UNKNOWN",
            }
            upload_unknown = code == "BATCH-UPLOAD-UNKNOWN" or (
                ambiguous and current is not None and str(current["status"]) == "uploading"
            )
            local_failure = code in {
                "BATCH-INPUT-002",
                "BATCH-INPUT-001",
                "BATCH-CLAIM-STALE",
                "BATCH-JOB-START-001",
            }
            if code == "BATCH-ALREADY-CLAIMED" or (code == "BATCH-CLAIM-STALE" and not remote_created):
                # A losing claimant must not overwrite the winner's state.
                raise
            if remote_created and submission_attempt:
                # The remote POST returned, but the local CAS/transaction did
                # not complete.  The remote identity is therefore unknown to
                # SQLite and must be held for manual ownership recovery; it
                # must never be treated as a definite HTTP rejection.
                self.batches.mark_submission_unknown(
                    batch_id,
                    submission_attempt,
                    owner,
                    "submission_persist_unknown",
                    str(exc),
                )
            elif ambiguous and upload_unknown and upload_attempt:
                self.batches.mark_upload_unknown(batch_id, upload_attempt, owner, "upload_unknown", str(exc))
            elif ambiguous and submission_attempt:
                self.batches.mark_submission_unknown(
                    batch_id, submission_attempt, owner, "submission_unknown", str(exc)
                )
            elif has_uploaded_file:
                # A definite HTTP rejection leaves the uploaded input file to
                # the cleanup worker; it is never silently retried as a new Batch.
                self.batches.update_batch(
                    batch_id,
                    status="cleanup_pending",
                    remote_status="failed",
                    cleanup_status="pending",
                    last_error_code=code,
                    last_error_message=str(exc)[:1000],
                    phase_started_at=utc_now(),
                )
                for item in self.batches.items(batch_id):
                    if str(item["status"]) in {"pending", "submitted"}:
                        self.batches.update_item(
                            str(item["id"]),
                            status="retry_pending",
                            error_code=code,
                            error_message=str(exc)[:1000],
                        )
                        if item.get("job_item_id"):
                            self.jobs.fail_batch_item(
                                str(current["job_id"]),
                                str(item["job_item_id"]),
                                code,
                                str(exc),
                            )
                self.batches.release_side_effect_claim(batch_id, owner)
            elif local_failure or not has_uploaded_file:
                self.batches.fail_local_batch(batch_id, code, str(exc))
            raise
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def submit(
        self,
        *,
        scope: str = "sample",
        sample_count: int = 100,
        photo_ids: Iterable[str] | None = None,
        created_by: str | None = None,
        budget_limit: float | None = None,
    ) -> dict[str, Any]:
        submit_started = time.monotonic()
        plan, analysis_fp, model = self._plan()
        provider_id = str(plan["provider_route"][0]["provider_id"])
        provider_identity = self._provider_identity(provider_id)
        candidates, skipped, seed = self._candidate_rows(
            scope=scope,
            analysis_fingerprint=analysis_fp,
            provider_id=provider_id,
            model=model,
            plan=plan,
            photo_ids=photo_ids,
            sample_count=sample_count,
        )
        if not candidates:
            raise BatchLifecycleError("目前沒有符合條件的 Batch 候選", "BATCH-CANDIDATE-001")
        estimate = self._estimate_candidates(
            scope=scope,
            sample_seed=seed,
            candidates=candidates,
            skipped=skipped,
            model=model,
            analysis_fingerprint=analysis_fp,
            provider_id=provider_id,
        )
        if estimate["estimated_cost"] is None:
            raise BatchLifecycleError("Batch 模型價格不完整，無法安全估算成本；請先補齊 input/cached/output 定價", "BATCH-COST-UNKNOWN")
        if budget_limit is not None and estimate["estimated_cost"] > float(budget_limit):
            raise BatchLifecycleError("整批估算成本超過 Job Budget，未提交任何分片", "BATCH-BUDGET-001")
        try:
            job_id = self.jobs.create(
                kind="analysis_batch",
                name=f"OpenAI Batch：{scope}",
                strategy="single",
                settings={"processing_mode": "batch", "scope": scope, "sample_seed": seed},
                photo_ids=[str(item["photo"]["id"]) for item in candidates],
                created_by=created_by,
                budget_limit=budget_limit,
                selection_mode=scope,
                analysis_fingerprint=analysis_fp,
                analysis_spec=plan,
            )
        except AnalysisReservationConflict as exc:
            raise BatchLifecycleError(str(exc), "BATCH-RESERVATION-CONFLICT") from exc
        job_items = self.jobs.list_items(job_id, limit=max(100, len(candidates)))
        job_items_by_photo = {str(item["photo_id"]): item for item in job_items}
        estimated_each = float(estimate["estimated_cost"] or 0) / max(1, len(candidates))
        batch_id = str(uuid4())
        item_rows = []
        for candidate in candidates:
            photo_id = str(candidate["photo"]["id"])
            item_rows.append(
                {
                    "id": str(uuid4()),
                    "job_item_id": str(job_items_by_photo[photo_id]["id"]),
                    "photo_id": photo_id,
                    "custom_id": f"ibt:{uuid4()}",
                    "content_sha256": candidate["content_sha256"],
                    "analysis_fingerprint": analysis_fp,
                    "vision_request_fingerprint": candidate["vision_request_fingerprint"],
                    "vision_input_spec_json": candidate["vision_input_spec_json"],
                    "estimated_cost": estimated_each,
                }
            )
        try:
            self.batches.create_with_items(
                {
                    "id": batch_id,
                    "job_id": job_id,
                    "provider_id": provider_id,
                    "model": model,
                    "endpoint": self.ENDPOINT,
                    "analysis_fingerprint": analysis_fp,
                    "estimated_cost": estimate["estimated_cost"],
                    "sample_seed": seed,
                    "candidate_snapshot_json": self.batches.snapshot_json(
                        [str(item["photo"]["id"]) for item in candidates]
                    ),
                    "scope": scope,
                    **provider_identity,
                },
                item_rows,
            )
        except sqlite3.IntegrityError as exc:
            # The partial unique reservation indexes serialize concurrent
            # submits.  The losing Job was never started and is explicitly
            # closed so it cannot become an orphan pending/running Job.
            self.jobs.abandon_unstarted(job_id)
            raise BatchLifecycleError(
                "候選照片已被另一個 Batch reservation 取得，未建立第二個遠端 Batch",
                "BATCH-RESERVATION-CONFLICT",
            ) from exc
        try:
            self.job_service.start(job_id)
        except Exception as exc:
            self.jobs.abandon_unstarted(job_id, "BATCH-JOB-START-001")
            self.batches.fail_local_batch(batch_id, "BATCH-JOB-START-001", str(exc))
            self._cleanup_local(self.batches.get(batch_id) or {}, immediate=True)
            raise
        provider = None
        try:
            self._activity(
                "INFO",
                "batch_prepare",
                "Batch JSONL preparation started",
                batch_id=batch_id,
                job_id=job_id,
                count=len(candidates),
            )
            provider = self._provider(provider_id, plan)
            batch_ids = self._prepare_shards(batch_id, candidates, plan, provider)
            self._activity(
                "INFO",
                "batch_prepared",
                "Batch JSONL preparation completed",
                batch_id=batch_id,
                job_id=job_id,
                count=len(batch_ids),
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "BATCH-INPUT-001"))
            self.batches.fail_local_batch(batch_id, code, str(exc), include_job_siblings=True)
            for local_batch in self.batches.list_for_job(job_id):
                self._cleanup_local(local_batch, immediate=True)
            raise
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        submitted: list[str] = []
        for current_id in batch_ids:
            current = self.batches.get(current_id)
            if current is None:
                continue
            try:
                self._submit_one(current_id, plan)
                submitted.append(current_id)
            except Exception as exc:
                self._activity(
                    "WARNING",
                    "batch_submit_failed",
                    "Batch 分片提交失敗；保留待重試狀態",
                    batch_id=current_id,
                    error_code=str(getattr(exc, "code", "BATCH-SUBMIT-001")),
                )
                continue
        log_event(
            LOGGER,
            logging.INFO,
            "Batch submission pass completed",
            event="batch_submit_completed",
            batch_id=batch_id,
            job_id=job_id,
            provider_id=provider_id,
            model=model,
            duration_ms=int((time.monotonic() - submit_started) * 1000),
            details={"prepared_shards": len(batch_ids), "submitted_shards": len(submitted)},
        )
        return {
            "job_id": job_id,
            "batch_ids": submitted,
            "prepared_batch_ids": batch_ids,
            "candidate_count": len(candidates),
            "cache_hits": skipped["cache_hits"],
            "sha_duplicates": skipped["sha_duplicates"],
            "never_upload_excluded": skipped["never_upload_excluded"],
            "analysis_fingerprint": analysis_fp,
            "model": model,
        }

    def _enqueue_import(self, batch_id: str, *, cleanup_only: bool = False) -> str:
        job_id = self.jobs.create_maintenance(
            kind="analysis_batch_import",
            name=f"匯入 OpenAI Batch {batch_id}",
            settings={"batch_id": batch_id, "cleanup_only": cleanup_only},
            created_by=None,
            priority=2,
            dedupe_key=f"analysis-batch-import:{batch_id}:{'cleanup' if cleanup_only else 'import'}",
        )
        current = self.jobs.get(job_id)
        if current is not None and str(current["status"]) == "pending":
            self.job_service.start(job_id)
        return job_id

    def _phase_is_stale(self, batch: dict[str, Any]) -> bool:
        batch = dict(batch)
        raw = str(batch.get("phase_started_at") or batch.get("updated_at") or "")
        try:
            started = datetime.fromisoformat(raw).timestamp()
        except (TypeError, ValueError, OverflowError):
            return True
        timeout = max(60, int(self.settings.get("batch.recovery_timeout_seconds", 900)))
        return time.time() - started >= timeout

    def _mark_unknown_after_restart(self, batch: dict[str, Any], *, upload: bool) -> None:
        batch = dict(batch)
        if str(batch["status"]) in TERMINAL_BATCH_STATUSES:
            return
        state = "upload_unknown" if upload else "submission_unknown"
        self._activity(
            "WARNING",
            "batch_restart_recovery",
            "Batch external side effect entered restart recovery hold",
            batch_id=str(batch["id"]),
            status=state,
        )
        self.batches.update_batch(
            str(batch["id"]),
            status=state,
            remote_status=state,
            cleanup_status="pending" if upload else batch.get("cleanup_status"),
            reconciliation_error_code=state,
            reconciliation_error_message="程序重啟後外部 side effect 結果未知，等待管理員驗證",
            completed_at=None,
            phase_started_at=utc_now(),
        )
        for item in self.batches.items(str(batch["id"])):
            if str(item["status"]) in {"pending", "submitted"}:
                self.batches.update_item(
                    str(item["id"]), status=state, error_code=state, error_message="restart recovery hold"
                )

    def _handle_frozen_plan_failure(self, batch_id: str, error_code: str, message: str) -> bool:
        """Leave a malformed frozen plan in a convergent cleanup state.

        A plan parse failure is a definite local failure only while no remote
        side effect exists.  Once an input File or Batch identity is present,
        the reservation must remain represented until the cleanup worker has
        processed the known files; the items are failed so that ``_finish``
        cannot leave the parent Job running forever.
        """

        batch = self.batches.get(batch_id)
        if batch is None:
            return False
        batch = dict(batch)
        original_status = str(batch["status"])
        has_side_effect = any(
            batch[key] for key in ("input_file_id", "remote_batch_id", "output_file_id", "error_file_id")
        )
        if not has_side_effect:
            if original_status in TERMINAL_BATCH_STATUSES:
                self._hold_terminal_cleanup_for_invalid_plan(
                    batch,
                    error_code=error_code,
                    error_message=message,
                    cleanup_status="not_required",
                )
                return False
            self.batches.fail_local_batch(batch_id, error_code, message)
            self._cleanup_local(batch, immediate=True)
            return False
        if original_status in TERMINAL_BATCH_STATUSES:
            has_file_ids = any(batch[key] for key in ("input_file_id", "output_file_id", "error_file_id"))
            unresolved_files = any(
                batch[file_key] and not bool(batch[deleted_key])
                for file_key, deleted_key in (
                    ("input_file_id", "input_file_deleted"),
                    ("output_file_id", "output_file_deleted"),
                    ("error_file_id", "error_file_deleted"),
                )
            )
            cleanup_status = (
                "pending" if unresolved_files else ("completed" if has_file_ids else "not_required")
            )
            self.batches.update_batch(
                batch_id,
                cleanup_status=cleanup_status,
                cleanup_error_code=error_code,
                cleanup_error_message=message[:1000],
            )
            self._enqueue_import(batch_id, cleanup_only=True)
            return True
        self.batches.update_batch(
            batch_id,
            status="cleanup_pending",
            remote_status="failed",
            cleanup_status="pending",
            cleanup_final_action="fail",
            reconciliation_error_code=error_code,
            reconciliation_error_message=message[:1000],
            completed_at=None,
        )
        self._enqueue_import(batch_id, cleanup_only=True)
        return True

    def enqueue_poll(self) -> str | None:
        """Schedule bounded remote work; the Scheduler only reads/writes SQLite."""
        active_sql = """SELECT id,status FROM jobs WHERE kind='analysis_batch_poll'
            AND status IN ('pending','preparing','running','pausing','retrying','paused','budget_exceeded')
            ORDER BY created_at,id LIMIT 1"""
        with self.database.session() as connection:
            active = connection.execute(active_sql).fetchone()
        if active is not None:
            if active["status"] == "pending":
                self.job_service.start(str(active["id"]))
            return str(active["id"])
        if not self.batches.list_pollable_due(limit=1):
            return None
        job_id = self.jobs.create_maintenance_atomic(
            kind="analysis_batch_poll",
            name="Batch 狀態輪詢",
            settings={"trigger_source": "scheduler", "max_attempts": 1},
            created_by=None,
            priority=4,
            transaction_guard=lambda connection: connection.execute(active_sql).fetchone() is None,
        )
        if job_id is not None:
            self.job_service.start(job_id)
        return job_id

    def poll_due(self, *, limit: int = 20) -> dict[str, int]:
        rows = self.batches.list_pollable_due(limit=limit)
        poll_started = time.monotonic()
        log_event(
            LOGGER,
            logging.DEBUG,
            "Batch poll pass started",
            event="batch_poll",
            operation="batch_poll",
            details={"candidate_count": len(rows), "limit": int(limit)},
        )
        polled = 0
        enqueued = 0
        for batch in rows:
            provider = None
            batch_id = str(batch["id"])
            owner = f"poll:{uuid4()}"
            lease_until = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
            if not self.batches.claim_poll(
                batch_id, owner, lease_until, int(batch.get("side_effect_version") or 0)
            ):
                continue
            try:
                current = self.batches.get(batch_id)
                if current is None:
                    continue
                status = str(current["status"])
                if status == "preparing":
                    if self._phase_is_stale(current):
                        if any(
                            current[key]
                            for key in ("input_file_id", "remote_batch_id", "output_file_id", "error_file_id")
                        ):
                            if self._handle_frozen_plan_failure(
                                batch_id,
                                "BATCH-RECOVERY-PREPARING-CLEANUP",
                                "準備階段已存在遠端檔案，停止並等待清理",
                            ):
                                enqueued += 1
                        else:
                            self.batches.fail_local_batch(
                                batch_id,
                                "BATCH-RECOVERY-PREPARING",
                                "JSONL preparation 在重啟前未完成",
                            )
                            self._cleanup_local(self.batches.get(batch_id) or {}, immediate=True)
                    continue
                if status == "uploading":
                    if self._phase_is_stale(current):
                        if current["input_file_id"]:
                            self.batches.update_batch(
                                batch_id,
                                status="uploaded",
                                remote_status="uploaded",
                                last_error_code=None,
                                last_error_message=None,
                                phase_started_at=utc_now(),
                            )
                            for item in self.batches.items(batch_id, statuses={"uploading"}):
                                self.batches.update_item(str(item["id"]), status="pending")
                        else:
                            self._mark_unknown_after_restart(current, upload=True)
                    continue
                if status == "uploaded":
                    plan_row = self.jobs.get(str(current["job_id"])) if current["job_id"] else None
                    self.batches.release_side_effect_claim(batch_id, owner)
                    try:
                        plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
                        if plan_row and (not isinstance(plan, dict) or not plan.get("provider_route")):
                            raise BatchLifecycleError(
                                "Frozen Analysis Plan JSON 缺少有效 provider route", "BATCH-POLL-PLAN-001"
                            )
                        self._submit_one(batch_id, plan)
                    except BatchLifecycleError as exc:
                        if exc.code == "BATCH-POLL-PLAN-001":
                            if self._handle_frozen_plan_failure(batch_id, exc.code, str(exc)):
                                enqueued += 1
                        elif exc.code not in {"BATCH-ALREADY-CLAIMED", "BATCH-CLAIM-STALE"}:
                            self.batches.update_batch(
                                batch_id,
                                last_error_code=exc.code,
                                last_error_message=str(exc)[:1000],
                            )
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        if self._handle_frozen_plan_failure(batch_id, "BATCH-POLL-PLAN-001", str(exc)):
                            enqueued += 1
                    except Exception as exc:
                        self.batches.update_batch(
                            batch_id,
                            last_error_code=str(getattr(exc, "code", "BATCH-RECOVERY-UPLOAD-001")),
                            last_error_message=str(exc)[:1000],
                        )
                    continue
                if status == "submitting":
                    if self._phase_is_stale(current):
                        if current["remote_batch_id"]:
                            self.batches.update_batch(
                                batch_id,
                                status="validating",
                                remote_status="validating",
                                last_error_code=None,
                                last_error_message=None,
                                phase_started_at=utc_now(),
                            )
                        else:
                            self._mark_unknown_after_restart(current, upload=False)
                    continue
                if status == "validating" and not current["remote_batch_id"]:
                    self._mark_unknown_after_restart(current, upload=False)
                    continue
                if status == "cleanup_pending":
                    self._enqueue_import(batch_id, cleanup_only=True)
                    enqueued += 1
                    continue
                if status in {"import_pending", "importing"}:
                    self._enqueue_import(batch_id)
                    enqueued += 1
                    continue
                remote_id = str(current["remote_batch_id"] or "")
                if not remote_id:
                    continue
                plan_row = self.jobs.get(str(current["job_id"])) if current["job_id"] else None
                try:
                    plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
                    if plan_row and (not isinstance(plan, dict) or not plan.get("provider_route")):
                        raise ValueError("Frozen Analysis Plan JSON 缺少有效 provider route")
                except (TypeError, ValueError, json.JSONDecodeError):
                    if self._handle_frozen_plan_failure(
                        batch_id,
                        "BATCH-POLL-PLAN-001",
                        "Frozen Analysis Plan JSON 無法解析或缺少有效 provider route",
                    ):
                        enqueued += 1
                    continue
                try:
                    provider = self._provider(str(current["provider_id"]), plan)
                    remote = provider.retrieve_batch(remote_id)
                    claimed = self.batches.get(batch_id)
                    state = self.batches.set_status_from_remote(
                        batch_id,
                        remote,
                        expected_version=int(claimed["side_effect_version"] or 0) if claimed else None,
                        owner=owner,
                    )
                    polled += 1
                    if state == "import_pending":
                        self._enqueue_import(batch_id)
                        enqueued += 1
                except Exception as exc:
                    if str(getattr(exc, "code", "")) not in {"BATCH-ALREADY-CLAIMED", "BATCH-CLAIM-STALE"}:
                        current_error = self.batches.get(batch_id)
                        if (
                            current_error is not None
                            and str(current_error["side_effect_owner"] or "") == owner
                        ):
                            self.batches.update_batch(
                                batch_id,
                                last_error_code=str(getattr(exc, "code", "BATCH-POLL-001")),
                                last_error_message=str(exc)[:1000],
                            )
                finally:
                    if provider is not None and callable(getattr(provider, "close", None)):
                        provider.close()
            except Exception as exc:
                current_error = self.batches.get(batch_id)
                if current_error is not None and str(current_error["side_effect_owner"] or "") == owner:
                    self.batches.update_batch(
                        batch_id,
                        last_error_code=str(getattr(exc, "code", "BATCH-POLL-001")),
                        last_error_message=str(exc)[:1000],
                    )
                self._activity(
                    "ERROR",
                    "batch_poll_iteration_failed",
                    "Batch poll iteration failed",
                    batch_id=batch_id,
                    error_code=str(getattr(exc, "code", "BATCH-POLL-001")),
                )
            finally:
                self.batches.release_side_effect_claim(batch_id, owner)
        log_event(
            LOGGER,
            logging.DEBUG,
            "Batch poll pass completed",
            event="batch_poll_completed",
            operation="batch_poll",
            duration_ms=int((time.monotonic() - poll_started) * 1000),
            details={"polled": polled, "enqueued": enqueued},
        )
        return {"polled": polled, "enqueued": enqueued}

    def cancel(self, batch_id: str) -> dict[str, Any]:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        batch = dict(batch)
        if str(batch["status"]) in TERMINAL_BATCH_STATUSES:
            file_entries = (
                ("input_file_id", "input_file_deleted"),
                ("output_file_id", "output_file_deleted"),
                ("error_file_id", "error_file_deleted"),
            )
            has_file_ids = any(batch[file_key] for file_key, _ in file_entries)
            unresolved_files = any(
                batch[file_key] and not bool(batch[deleted_key]) for file_key, deleted_key in file_entries
            )
            if unresolved_files:
                self.batches.update_batch(
                    batch_id,
                    cleanup_status="pending",
                    cleanup_final_action="cancel"
                    if str(batch["status"]) == "cancelled"
                    else self._cleanup_action_for(batch),
                )
                self._enqueue_import(batch_id, cleanup_only=True)
                return {
                    "status": str(batch["status"]),
                    "cleanup_pending": True,
                    "cleanup_retry": True,
                }
            if has_file_ids and str(batch["cleanup_status"]) != "completed":
                self.batches.update_batch(
                    batch_id, cleanup_status="completed", cleanup_completed_at=utc_now()
                )
            elif not has_file_ids and str(batch["cleanup_status"]) != "not_required":
                self.batches.update_batch(
                    batch_id, cleanup_status="not_required", cleanup_completed_at=utc_now()
                )
            return {"status": str(batch["status"]), "already_terminal": True}
        plan_row = self.jobs.get(str(batch["job_id"])) if batch["job_id"] else None
        plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
        if not batch["remote_batch_id"]:
            if str(batch["status"]) in {"upload_unknown", "submission_unknown", "submitting", "uploading"}:
                raise BatchLifecycleError(
                    "外部 side effect 結果未知，請使用 Confirm no remote Batch / Abandon",
                    "BATCH-CANCEL-UNKNOWN",
                )
            has_files = any(batch[key] for key in ("input_file_id", "output_file_id", "error_file_id"))
            if has_files:
                self.batches.update_batch(
                    batch_id,
                    status="cleanup_pending",
                    remote_status="cancelled",
                    cleanup_status="pending",
                    cleanup_final_action="cancel",
                )
                self._enqueue_import(batch_id, cleanup_only=True)
                return {"status": "cleanup_pending", "local_only": True}
            self.batches.finalize_batch_result(
                batch_id,
                status="cancelled",
                cleanup_final_action="cancel",
                cleanup_status="not_required",
                error_code="cancelled",
                error_message="local-only cancellation",
            )
            return {"status": "cancelled", "local_only": True}
        provider = self._provider(str(batch["provider_id"]), plan)
        owner = f"cancel:{uuid4()}"
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
        try:
            if not self.batches.claim_cancel(batch_id, owner, lease_until):
                return {"status": "already_claimed", "batch_id": batch_id}
            self.batches.update_batch(batch_id, cleanup_final_action="cancel")
            remote = provider.cancel_batch(str(batch["remote_batch_id"]))
            claimed = self.batches.get(batch_id)
            state = self.batches.set_status_from_remote(
                batch_id,
                remote,
                expected_version=int(claimed["side_effect_version"] or 0) if claimed else None,
                owner=owner,
            )
            if state == "import_pending":
                self._enqueue_import(batch_id)
            return {"status": str(self.batches.get(batch_id)["status"])}
        finally:
            self.batches.release_side_effect_claim(batch_id, owner)
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def abandon(
        self,
        batch_id: str,
        *,
        confirmed_no_remote: bool,
        remote_file_id: str | None = None,
        confirmed_remote_file_deleted: bool = False,
    ) -> dict[str, Any]:
        """Explicitly abandon an unknown submission after human confirmation."""

        if confirmed_no_remote is not True:
            raise BatchLifecycleError("Abandon 必須明確確認遠端 Batch 不存在", "BATCH-ABANDON-CONFIRM-001")
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        batch = dict(batch)
        if batch["remote_batch_id"]:
            raise BatchLifecycleError("已有遠端 Batch ID，不可 Abandon", "BATCH-ABANDON-REMOTE-001")
        if str(batch["status"]) not in {
            "upload_unknown",
            "submission_unknown",
            "submitting",
            "uploading",
            "uploaded",
            "validating",
        }:
            raise BatchLifecycleError("目前 Batch 不在可 Abandon 階段", "BATCH-ABANDON-002")
        if str(batch["status"]) == "upload_unknown":
            if remote_file_id:
                file_id = str(remote_file_id).strip()
                if not REMOTE_BATCH_ID_RE.fullmatch(file_id):
                    raise BatchLifecycleError("遠端 File ID 格式不合法", "BATCH-ABANDON-FILE-001")
                plan_row = self.jobs.get(str(batch["job_id"])) if batch["job_id"] else None
                try:
                    plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise BatchLifecycleError(
                        "Frozen Analysis Plan JSON 無法解析", "BATCH-ABANDON-FILE-002"
                    ) from exc
                if not isinstance(plan, dict):
                    raise BatchLifecycleError(
                        "Frozen Analysis Plan JSON 必須是物件", "BATCH-ABANDON-FILE-002"
                    )
                # Metadata validation and DELETE are intentionally separate:
                # a 404 is success only after ownership was proven.
                self._retrieve_and_validate_remote_file(batch, file_id, plan)
                self._delete_verified_remote_file(batch, file_id, plan)
                self.batches.abandon_unknown_upload(batch_id, confirmed_deleted=True)
                self._cleanup_local(self.batches.get(batch_id) or {}, immediate=True)
                return {"status": "failed", "batch_id": batch_id, "remote_file_deleted": True}
            if confirmed_remote_file_deleted is not True:
                raise BatchLifecycleError(
                    "upload_unknown 必須提供已驗證 remote_file_id 並刪除，或明確確認遠端 File 已刪除",
                    "BATCH-ABANDON-FILE-007",
                )
            self.batches.abandon_unknown_upload(batch_id, confirmed_deleted=True)
            self._cleanup_local(self.batches.get(batch_id) or {}, immediate=True)
            return {"status": "failed", "batch_id": batch_id, "remote_file_deleted": True}
        has_files = any(batch[key] for key in ("input_file_id", "output_file_id", "error_file_id"))
        if has_files:
            self.batches.update_batch(
                batch_id,
                abandon_confirmed_at=utc_now(),
                status="cleanup_pending",
                remote_status="abandoned",
                cleanup_status="pending",
                cleanup_final_action="abandon",
            )
        else:
            self.batches.finalize_batch_result(
                batch_id,
                status="failed",
                cleanup_final_action="abandon",
                cleanup_status="not_required",
                error_code="abandoned",
                error_message="管理員已確認遠端 Batch 不存在",
            )
        if has_files:
            self._enqueue_import(batch_id, cleanup_only=True)
            return {"status": "cleanup_pending", "batch_id": batch_id}
        return {"status": str(self.batches.get(batch_id)["status"]), "batch_id": batch_id}

    def retry_failed(self, batch_id: str, *, created_by: str | None = None) -> dict[str, Any]:
        items = self.batches.items(batch_id)
        retry_statuses = {
            "failed",
            "missing",
            "retry_pending",
            "schema_invalid",
            "duplicate_custom_id",
            "unexpected_custom_id",
        }
        photo_ids = [
            str(item["photo_id"])
            for item in (dict(item) for item in items)
            if str(item["status"]) in retry_statuses
            and str(item.get("error_code") or "") != "submission_unknown"
            and item["photo_id"]
        ]
        if not photo_ids:
            raise BatchLifecycleError("此 Batch 沒有可重試項目", "BATCH-RETRY-001")
        return self.submit(scope="manual_selection", photo_ids=photo_ids, created_by=created_by)

    def recover_uploaded_file(self, batch_id: str, remote_file_id: str) -> dict[str, Any]:
        """Bind a verified remote input File after an ambiguous upload.

        File recovery is intentionally separate from Batch recovery.  A missing
        remote Batch does not prove that the preceding File upload failed.
        """

        file_id = str(remote_file_id or "").strip()
        if not REMOTE_BATCH_ID_RE.fullmatch(file_id):
            raise BatchLifecycleError("遠端 File ID 格式不合法", "BATCH-UPLOAD-RECOVERY-001")
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        batch = dict(batch)
        if str(batch["status"]) != "upload_unknown":
            raise BatchLifecycleError("目前 Batch 不在 upload_unknown", "BATCH-UPLOAD-RECOVERY-002")
        plan_row = self.jobs.get(str(batch["job_id"])) if batch["job_id"] else None
        try:
            plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BatchLifecycleError(
                "Frozen Analysis Plan JSON 無法解析", "BATCH-UPLOAD-RECOVERY-PLAN-001"
            ) from exc
        if not isinstance(plan, dict):
            raise BatchLifecycleError(
                "Frozen Analysis Plan JSON 必須是物件", "BATCH-UPLOAD-RECOVERY-PLAN-001"
            )
        matched, _code, message = self._cleanup_identity_matches(batch)
        if not matched:
            raise BatchLifecycleError(message, "BATCH-UPLOAD-RECOVERY-003")
        route = next(
            (
                item
                for item in plan.get("provider_route", [])
                if str(item.get("provider_id")) == str(batch["provider_id"])
            ),
            None,
        )
        if not route or str(route.get("config_revision") or "") != str(
            batch.get("provider_config_revision") or ""
        ):
            raise BatchLifecycleError(
                "Provider/project context 已變更，拒絕 File Recovery", "BATCH-UPLOAD-RECOVERY-003"
            )
        provider = None
        try:
            provider = self._provider(str(batch["provider_id"]), plan)
            retrieve_file = getattr(provider, "retrieve_file", None)
            if not callable(retrieve_file):
                raise BatchLifecycleError(
                    "Provider 不支援遠端 File metadata Recovery", "BATCH-UPLOAD-RECOVERY-004"
                )
            remote = retrieve_file(file_id)
        finally:
            if provider is not None and callable(getattr(provider, "close", None)):
                provider.close()
        if not isinstance(remote, dict) or str(remote.get("id") or "") != file_id:
            raise BatchLifecycleError("遠端 File ID 與人工輸入不一致", "BATCH-UPLOAD-RECOVERY-005")
        if str(remote.get("purpose") or "") != "batch":
            raise BatchLifecycleError("遠端 File purpose 不是 batch", "BATCH-UPLOAD-RECOVERY-006")
        expected_name = f"inktime-batch-{batch_id}.jsonl"
        if str(remote.get("filename") or "") != expected_name:
            raise BatchLifecycleError(
                "遠端 File filename 不符合本機 Batch identity", "BATCH-UPLOAD-RECOVERY-007"
            )
        remote_provider = remote.get("provider_id") or remote.get("provider")
        if remote_provider is not None and str(remote_provider) != str(batch["provider_id"]):
            raise BatchLifecycleError("遠端 File Provider context 不符合本機", "BATCH-UPLOAD-RECOVERY-008")
        remote_project = remote.get("project_id") or remote.get("project")
        if remote_project is not None and str(remote_project) != str(batch.get("provider_project_id") or ""):
            raise BatchLifecycleError(
                "遠端 File project context 不符合 Frozen Provider", "BATCH-UPLOAD-RECOVERY-008"
            )
        local_bytes = batch.get("input_file_bytes")
        if local_bytes is None and batch.get("local_input_path"):
            path = Path(str(batch["local_input_path"]))
            if path.is_file():
                local_bytes = path.stat().st_size
        if remote.get("bytes") is not None:
            if local_bytes is None:
                raise BatchLifecycleError(
                    "無法取得本機 JSONL bytes，拒絕 File Recovery", "BATCH-UPLOAD-RECOVERY-009"
                )
            if int(remote.get("bytes") or 0) != int(local_bytes):
                raise BatchLifecycleError("遠端 File bytes 不符合本機 JSONL", "BATCH-UPLOAD-RECOVERY-009")
        with self.database.transaction() as connection:
            other = connection.execute(
                "SELECT id FROM analysis_batches WHERE input_file_id=? AND id<>?",
                (file_id, batch_id),
            ).fetchone()
            if other is not None:
                raise BatchLifecycleError("遠端 File 已綁定其他本機 Batch", "BATCH-UPLOAD-RECOVERY-010")
            if not self.batches.recover_uploaded_file(
                batch_id,
                file_id,
                int(local_bytes) if local_bytes is not None else None,
                connection=connection,
            ):
                raise BatchLifecycleError("Batch File Recovery claim 已失效", "BATCH-UPLOAD-RECOVERY-011")
        self._activity(
            "INFO",
            "batch_upload_recovered",
            "已綁定人工驗證的遠端 input File",
            batch_id=batch_id,
            status="uploaded",
        )
        return {"batch_id": batch_id, "input_file_id": file_id, "status": "uploaded"}

    def recover_submission(self, batch_id: str, remote_batch_id: str) -> dict[str, Any]:
        """Bind a manually confirmed remote Batch without creating another one.

        The remote object is fetched with the frozen Provider route first.  No
        local mutation or cleanup is allowed until every ownership assertion
        succeeds.
        """

        remote_id = str(remote_batch_id or "").strip()
        if not REMOTE_BATCH_ID_RE.fullmatch(remote_id):
            raise BatchLifecycleError("遠端 Batch ID 格式不合法", "BATCH-RECOVERY-001")
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        batch = dict(batch)
        if str(batch["status"]) not in {"submission_unknown", "submitting", "validating"}:
            raise BatchLifecycleError("目前 Batch 不在可 Recovery 的提交階段", "BATCH-RECOVERY-002")
        if str(batch["status"]) == "validating" and batch["remote_batch_id"]:
            raise BatchLifecycleError("Batch 已有遠端 ID，應由 Poll 流程處理", "BATCH-RECOVERY-002")
        plan_row = self.jobs.get(str(batch["job_id"])) if batch["job_id"] else None
        try:
            plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BatchLifecycleError(
                "Frozen Analysis Plan JSON 無法解析", "BATCH-RECOVERY-PLAN-001"
            ) from exc
        if not isinstance(plan, dict):
            raise BatchLifecycleError("Frozen Analysis Plan JSON 必須是物件", "BATCH-RECOVERY-PLAN-001")
        matched, _code, message = self._cleanup_identity_matches(batch)
        if not matched:
            raise BatchLifecycleError(message, "BATCH-RECOVERY-OWNERSHIP-007")
        provider = None
        try:
            provider = self._provider(str(batch["provider_id"]), plan)
            remote = provider.retrieve_batch(remote_id)
        finally:
            if provider is not None and callable(getattr(provider, "close", None)):
                provider.close()
        if not isinstance(remote, dict) or str(remote.get("id") or "") != remote_id:
            raise BatchLifecycleError("遠端 Batch ID 與人工輸入不一致", "BATCH-RECOVERY-OWNERSHIP-001")
        if str(remote.get("endpoint") or "") != self.ENDPOINT:
            raise BatchLifecycleError("遠端 Batch endpoint 不符合本機契約", "BATCH-RECOVERY-OWNERSHIP-002")
        if str(remote.get("input_file_id") or "") != str(batch["input_file_id"] or ""):
            raise BatchLifecycleError(
                "遠端 input_file_id 不符合本機已保存檔案", "BATCH-RECOVERY-OWNERSHIP-003"
            )
        metadata = remote.get("metadata")
        if not isinstance(metadata, dict):
            raise BatchLifecycleError("遠端 Batch 缺少 ownership metadata", "BATCH-RECOVERY-OWNERSHIP-004")
        if str(metadata.get("inktime_batch_id") or "") != batch_id:
            raise BatchLifecycleError(
                "遠端 metadata Batch ID 不符合本機 Batch", "BATCH-RECOVERY-OWNERSHIP-005"
            )
        if str(metadata.get("inktime_version") or "") != "batch-lifecycle-v1":
            raise BatchLifecycleError(
                "遠端 Batch lifecycle version 不符合本機契約", "BATCH-RECOVERY-OWNERSHIP-006"
            )
        route = next(
            (
                item
                for item in plan.get("provider_route", [])
                if str(item.get("provider_id")) == str(batch["provider_id"])
            ),
            None,
        )
        if not route or str(route.get("config_revision") or "") != str(
            batch.get("provider_config_revision") or ""
        ):
            raise BatchLifecycleError(
                "Provider/project context 已變更，拒絕 Recovery", "BATCH-RECOVERY-OWNERSHIP-007"
            )
        for key, remote_keys in (
            ("provider_id", ("provider_id", "provider")),
            ("project_id", ("project_id", "project")),
        ):
            remote_context = next(
                (remote.get(name) for name in remote_keys if remote.get(name) is not None), None
            )
            frozen_context = (
                batch.get("provider_project_id") if key == "project_id" else batch.get("provider_id")
            )
            if remote_context is not None and str(remote_context) != str(frozen_context or ""):
                raise BatchLifecycleError(
                    f"遠端 {key} context 不符合 Frozen Provider", "BATCH-RECOVERY-OWNERSHIP-007"
                )
        counts = remote.get("request_counts")
        if (
            isinstance(counts, dict)
            and "total" in counts
            and int(counts.get("total") or 0) != int(batch["total_items"] or 0)
        ):
            raise BatchLifecycleError(
                "遠端 request_counts.total 與本機 Batch 數量不一致", "BATCH-RECOVERY-OWNERSHIP-008"
            )
        existing = str(batch["remote_batch_id"] or "")
        if existing and existing != remote_id:
            raise BatchLifecycleError("Batch 已綁定其他遠端 ID", "BATCH-RECOVERY-003")
        with self.database.transaction() as connection:
            state = self.batches.bind_recovered_remote(batch_id, remote, connection=connection)
            if batch["job_id"] and not self.jobs.reopen_batch_job(
                str(batch["job_id"]), connection=connection
            ):
                raise BatchLifecycleError(
                    "Batch Recovery 無法原子重新開啟 Parent Job", "BATCH-RECOVERY-JOB-001"
                )
        self._activity(
            "INFO",
            "batch_submission_recovered",
            "已綁定人工確認的既有遠端 Batch",
            batch_id=batch_id,
            status=state,
        )
        if state == "import_pending":
            self._enqueue_import(batch_id)
        return {"batch_id": batch_id, "remote_batch_id": remote_id, "status": state}

    def retry_cleanup(self, batch_id: str) -> dict[str, Any]:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        batch = dict(batch)
        file_entries = (
            ("input_file_id", "input_file_deleted"),
            ("output_file_id", "output_file_deleted"),
            ("error_file_id", "error_file_deleted"),
        )
        has_file_ids = any(batch[file_key] for file_key, _ in file_entries)
        unresolved_files = any(
            batch[file_key] and not bool(batch[deleted_key]) for file_key, deleted_key in file_entries
        )
        terminal_status = str(batch["status"]) in TERMINAL_BATCH_STATUSES
        if str(batch["status"]) == "upload_unknown" and not batch["input_file_id"]:
            # An upload response can be lost after the remote File exists.
            # There is no safe automatic DELETE target until an administrator
            # validates it, so retaining the reservation is intentional.
            self.batches.update_batch(
                batch_id,
                cleanup_status="pending",
                cleanup_error_code="BATCH-CLEANUP-UPLOAD-UNKNOWN",
                cleanup_error_message="請先 Recover Uploaded File，或確認已驗證的 remote File 已刪除",
            )
            return {"status": "operator_action_required", "batch_id": batch_id}
        if str(batch["status"]) == "submission_unknown" and not batch["remote_batch_id"] and not has_file_ids:
            self.batches.update_batch(
                batch_id,
                cleanup_status="pending",
                cleanup_error_code="BATCH-CLEANUP-SUBMISSION-UNKNOWN",
                cleanup_error_message="請先 Bind verified remote Batch 或 Confirm no remote Batch / Abandon",
            )
            return {"status": "operator_action_required", "batch_id": batch_id}
        if not unresolved_files and has_file_ids and str(batch["cleanup_status"]) != "completed":
            self.batches.update_batch(batch_id, cleanup_status="completed", cleanup_completed_at=utc_now())
            batch["cleanup_status"] = "completed"
        elif not has_file_ids and str(batch["cleanup_status"]) != "not_required":
            self.batches.update_batch(batch_id, cleanup_status="not_required", cleanup_completed_at=utc_now())
            batch["cleanup_status"] = "not_required"
        if str(batch["cleanup_status"]) in {"completed", "not_required"} and not unresolved_files:
            current = dict(self.batches.get(batch_id) or batch)
            action = self._cleanup_action_for(current)
            if action in {"cancel", "abandon", "fail"}:
                self.batches.update_batch(batch_id, cleanup_final_action=action)
                if str(current["status"]) not in TERMINAL_BATCH_STATUSES:
                    self.batches.finalize_cleanup(batch_id)
                else:
                    self._finish(batch_id)
            elif action == "complete":
                self.batches.update_batch(batch_id, cleanup_final_action="complete")
                self._finish(batch_id)
            current = dict(self.batches.get(batch_id) or current)
            return {
                "status": str(current["cleanup_status"]),
                "already_cleaned": True,
                "batch_id": batch_id,
            }
        cleanup_changes = {
            "cleanup_status": "pending",
            "cleanup_final_action": self._cleanup_action_for(batch),
        }
        if not terminal_status:
            cleanup_changes["status"] = "cleanup_pending"
        self.batches.update_batch(batch_id, **cleanup_changes)
        return {
            "status": str(batch["status"]) if terminal_status else "cleanup_pending",
            "cleanup_pending": True,
            "job_id": self._enqueue_import(batch_id, cleanup_only=True),
            "batch_id": batch_id,
        }

    @staticmethod
    def _usage_from_body(body: dict[str, Any]) -> Usage:
        usage_value: Any = body.get("usage")
        usage: dict[str, Any] = usage_value if isinstance(usage_value, dict) else {}
        prompt_details_value = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
        prompt_details = prompt_details_value if isinstance(prompt_details_value, dict) else {}
        completion_details_value = usage.get("completion_tokens_details")
        completion_details = completion_details_value if isinstance(completion_details_value, dict) else {}
        def bounded_int(value: Any) -> int:
            if isinstance(value, bool):
                return 0
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError, OverflowError):
                return 0

        reported_cost = usage.get("cost")
        if reported_cost is None:
            reported_cost = body.get("cost")
        try:
            reported_cost = (
                float(reported_cost)
                if reported_cost is not None and not isinstance(reported_cost, bool)
                else None
            )
        except (TypeError, ValueError, OverflowError):
            reported_cost = None
        if reported_cost is not None and (not math.isfinite(reported_cost) or reported_cost < 0):
            reported_cost = None
        return Usage(
            bounded_int(usage.get("prompt_tokens", usage.get("input_tokens", 0))),
            bounded_int(usage.get("completion_tokens", usage.get("output_tokens", 0))),
            bounded_int(prompt_details.get("cached_tokens", usage.get("cached_tokens", 0))),
            bounded_int(completion_details.get("reasoning_tokens", usage.get("reasoning_tokens", 0))),
            bounded_int(
                prompt_details.get("cache_write_tokens", usage.get("cache_write_tokens", 0))
            ),
            reported_cost,
        )

    @staticmethod
    def _current_content_sha(photo) -> str | None:
        try:
            source = safe_join(Path(str(photo["root_path"])), str(photo["relative_path"]))
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except (OSError, ValueError):
            return None

    def _mark_item_error(
        self,
        item: dict[str, Any],
        code: str,
        message: str,
        status: str = "failed",
        *,
        job_id: str | None = None,
    ) -> None:
        if str(item["status"]) == "imported":
            return
        self.batches.update_item(
            str(item["id"]),
            status=status,
            error_code=code,
            error_message=str(message)[:1000],
        )
        if item.get("job_item_id") and job_id:
            self.jobs.fail_batch_item(str(job_id), str(item["job_item_id"]), code, str(message))

    def _import_success(
        self,
        batch: dict[str, Any],
        item: dict[str, Any],
        line: dict[str, Any],
        body: dict[str, Any],
        plan: dict[str, Any],
        provider,
        current_analysis_fingerprint: str | None,
    ) -> str:
        if str(item["status"]) == "imported":
            return "already_imported"
        if str(item["status"]) not in {"pending", "submitted", "retry_pending"}:
            return str(item["status"])
        submitted_fingerprint = str(batch["analysis_fingerprint"] or "")
        if (
            current_analysis_fingerprint != submitted_fingerprint
            or fingerprint(plan) != submitted_fingerprint
            or str(item["analysis_fingerprint"] or "") != submitted_fingerprint
        ):
            self._mark_item_error(
                item,
                "BATCH-STALE-ANALYSIS-FINGERPRINT",
                "目前 Analysis Fingerprint 已不同於送出時版本",
                "stale",
                job_id=str(batch["job_id"]),
            )
            return "stale"
        photo = self.photos.get_with_path(str(item["photo_id"]))
        if photo is None:
            self._mark_item_error(
                item, "BATCH-PHOTO-MISSING", "照片已不存在", "stale", job_id=str(batch["job_id"])
            )
            return "stale"
        current_sha = (self._current_content_sha(photo) or "").casefold()
        vision_input = json.loads(str(item["vision_input_spec_json"] or "{}"))
        current_fp = fingerprint(
            {
                "content_sha256": current_sha,
                "actual_provider": str(batch["provider_id"]),
                "model": str(batch["model"]),
                "prompt_version": str(plan["prompt_version"]),
                "schema_version": SCHEMA_VERSION,
                "schema_kind": "full",
                "provider_prompt_contract_sha256": str(plan.get("provider_prompt_contract_sha256") or ""),
                **vision_input,
            }
        )
        if current_sha != str(item["content_sha256"]).casefold() or current_fp != str(
            item["vision_request_fingerprint"]
        ):
            self._mark_item_error(
                item,
                "BATCH-STALE",
                "送出後照片內容或 Vision Request Fingerprint 已變更",
                "stale",
                job_id=str(batch["job_id"]),
            )
            return "stale"
        response = line.get("response") or {}
        request_id = response.get("request_id") or line.get("request_id")
        usage = self._usage_from_body(body)
        raw_content = (
            body.get("choices", [{}])[0].get("message", {}).get("content") if body.get("choices") else None
        )
        if isinstance(raw_content, list):
            raw_content = "".join(str(part.get("text", "")) for part in raw_content if isinstance(part, dict))
        if not isinstance(raw_content, str) or not raw_content.strip():
            self._mark_item_error(
                item,
                "BATCH-RESPONSE-BODY",
                "HTTP 200 但沒有有效 Response Body",
                "schema_invalid",
                job_id=str(batch["job_id"]),
            )
            return "schema_invalid"
        try:
            result = validate_model_response(json.loads(raw_content))
        except (ValueError, TypeError, json.JSONDecodeError, AnalysisValidationError) as exc:
            self._mark_item_error(
                item, "schema_invalid", str(exc), "schema_invalid", job_id=str(batch["job_id"])
            )
            return "schema_invalid"
        caption_controls = dict(plan.get("caption_controls") or {})
        caption_controls.update(dict(plan.get("caption_display_controls") or {}))
        result = self.analysis._apply_caption_variant(result, caption_controls or None)
        weights = dict(plan.get("ranking_weights") or {})
        estimated_cost = provider.estimate_batch_cost(str(batch["model"]), usage)
        actual_cost = usage.provider_reported_cost
        cost_source = "provider_reported" if actual_cost is not None else "estimated" if estimated_cost is not None else "unknown"
        recorded_cost = actual_cost if actual_cost is not None else estimated_cost
        raw_line = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction(operation="analysis_batch_import") as connection:
            current_item = connection.execute(
                "SELECT status FROM analysis_batch_items WHERE id=?",
                (str(item["id"]),),
            ).fetchone()
            if current_item is None:
                return "missing"
            current_item_status = str(current_item["status"])
            if current_item_status == "imported":
                return "already_imported"
            if current_item_status not in {"pending", "submitted", "retry_pending"}:
                return current_item_status
            ranked = self.analysis._save_result(
                photo_id=str(item["photo_id"]),
                job_id=str(batch["job_id"]) if batch["job_id"] else None,
                stage="single",
                provider=str(batch["provider_id"]),
                model=str(batch["model"]),
                result=result,
                raw=raw_content,
                photo=photo,
                ranking_weights=weights,
                favorite_bonus=float(plan.get("favorite_bonus", 0)),
                scoring_version_id=str(plan.get("scoring_profile_id") or "") or None,
                schema_kind="full",
                prompt_version=str(plan["prompt_version"]),
                analysis_fingerprint=str(batch["analysis_fingerprint"]),
                analysis_spec_json=canonical_json(plan),
                vision_request_fingerprint=str(item["vision_request_fingerprint"]),
                vision_input_spec_json=str(item["vision_input_spec_json"]),
                analysis_source="analysis_batch",
                connection=connection,
            )
            self.photos.put_ai_cache(
                content_sha256=str(item["content_sha256"]),
                provider=str(batch["provider_id"]),
                model_name=str(batch["model"]),
                prompt_version=str(plan["prompt_version"]),
                schema_version=SCHEMA_VERSION,
                schema_kind="full",
                result=ranked,
                raw_json=raw_content,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                estimated_cost=recorded_cost or 0.0,
                latency_ms=0,
                vision_request_fingerprint=str(item["vision_request_fingerprint"]),
                vision_input_spec_json=str(item["vision_input_spec_json"]),
                connection=connection,
            )
            self.usage.record_batch_once(
                provider=str(batch["provider_id"]),
                provider_id=str(batch["provider_id"]),
                model=str(batch["model"]),
                job_id=str(batch["job_id"]) if batch["job_id"] else None,
                photo_id=str(item["photo_id"]),
                batch_id=str(batch["id"]),
                batch_item_id=str(item["id"]),
                request_type="analysis_batch",
                input_tokens=usage.input_tokens,
                cached_tokens=usage.cached_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                estimated_cost=recorded_cost or 0.0,
                actual_cost=actual_cost,
                request_id=str(request_id) if request_id else None,
                started_at=str(batch["submitted_at"] or utc_now()),
                cache_write_tokens=usage.cache_write_tokens,
                cost_source=cost_source,
                connection=connection,
            )
            self.batches.update_item(
                str(item["id"]),
                connection=connection,
                status="imported",
                request_id=str(request_id) if request_id else None,
                http_status=int(response.get("status_code", 200) or 200),
                input_tokens=usage.input_tokens,
                cached_tokens=usage.cached_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                actual_cost=recorded_cost or 0.0,
                raw_response_json=raw_line,
                imported_at=utc_now(),
            )
            if item.get("job_item_id"):
                self.jobs.complete_batch_item(
                    str(batch["job_id"]),
                    str(item["job_item_id"]),
                    {"stage": "single", "processing_mode": "batch", "analysis": ranked},
                    recorded_cost or 0.0,
                    connection=connection,
                )

        from inktime.app.repositories.photos import invalidate_score_population_cache
        invalidate_score_population_cache()
        return "imported"

    def _read_results(
        self,
        path: Path | None,
        batch: dict[str, Any],
        *,
        is_error: bool,
        records: dict[str, tuple[str, dict[str, Any], dict[str, Any] | None]],
    ) -> None:
        if path is None or not path.is_file():
            return
        with path.open("r", encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, 1):
                if len(raw_line.encode("utf-8")) > 25 * 1024 * 1024:
                    raise BatchLifecycleError("結果 JSONL 單行超過解析上限", "BATCH-OUTPUT-LINE-001")
                try:
                    record = json.loads(raw_line)
                except (ValueError, json.JSONDecodeError):
                    self.batches.update_batch(
                        str(batch["id"]),
                        reconciliation_error_code="invalid_jsonl",
                        reconciliation_error_message=f"結果檔第 {line_number} 行不是有效 JSONL",
                    )
                    continue
                if not isinstance(record, dict):
                    self.batches.update_batch(
                        str(batch["id"]),
                        reconciliation_error_code="invalid_jsonl",
                        reconciliation_error_message=f"結果檔第 {line_number} 行不是 JSON Object",
                    )
                    continue
                custom_id = record.get("custom_id")
                if not isinstance(custom_id, str) or not CUSTOM_ID_RE.fullmatch(custom_id):
                    self.batches.update_batch(
                        str(batch["id"]),
                        reconciliation_error_code="unexpected_custom_id",
                        reconciliation_error_message=f"結果檔第 {line_number} 行含不合法 custom_id",
                    )
                    continue
                if custom_id in records:
                    records[custom_id] = ("duplicate", record, None)
                    continue
                if is_error or record.get("error") is not None:
                    records[custom_id] = ("error", record, None)
                    continue
                response = record.get("response")
                status_code = 0
                if isinstance(response, dict):
                    try:
                        status_code = int(response.get("status_code", 0) or 0)
                    except (TypeError, ValueError):
                        status_code = 0
                if not isinstance(response, dict) or status_code != 200:
                    records[custom_id] = ("error", record, None)
                    continue
                body = response.get("body")
                if not isinstance(body, dict):
                    records[custom_id] = ("schema_invalid", record, None)
                    continue
                records[custom_id] = ("success", record, body)

    def _cleanup_local(self, batch: dict[str, Any], *, immediate: bool = False) -> None:
        batch = dict(batch)
        retention_days = 0 if immediate else max(0, int(self.settings.get("batch.local_retention_days", 7)))
        cutoff = time.time() - (retention_days * 24 * 60 * 60)
        for key in ("local_input_path", "local_output_path", "local_error_path"):
            raw_path = batch.get(key)
            if not raw_path:
                continue
            path = Path(str(raw_path))
            try:
                if path.is_file() and (retention_days == 0 or path.stat().st_mtime <= cutoff):
                    path.unlink()
            except OSError:
                # Remote cleanup is already complete; retain the path for the next
                # local housekeeping pass rather than changing the analysis result.
                continue
        if immediate:
            batch_id = str(batch.get("id") or "")
            root = (self.batch_root / batch_id).resolve() if batch_id else None
            if root is not None and root.parent == self.batch_root.resolve() and root.is_dir():
                for candidate in sorted(root.rglob("*"), key=lambda value: len(value.parts), reverse=True):
                    try:
                        if candidate.is_file() or candidate.is_symlink():
                            candidate.unlink()
                        elif candidate.is_dir():
                            candidate.rmdir()
                    except OSError:
                        pass
                try:
                    root.rmdir()
                except OSError:
                    pass

    def _cleanup_remote(self, batch: dict[str, Any], plan: dict[str, Any]) -> bool:
        batch = dict(batch)

        files = (
            ("input", "input_file_id", "input_file_deleted"),
            ("output", "output_file_id", "output_file_deleted"),
            ("error", "error_file_id", "error_file_deleted"),
        )

        def complete_cleanup() -> None:
            current = self.batches.get(str(batch["id"]))
            if current is None:
                return
            current = dict(current)
            action = self._cleanup_action_for(current)
            if (
                action in {"cancel", "abandon", "fail"}
                and str(current["status"]) not in TERMINAL_BATCH_STATUSES
            ):
                self.batches.finalize_cleanup(str(batch["id"]))
            else:
                self._finish(str(batch["id"]))

        def complete_if_converged() -> bool:
            current = self.batches.get(str(batch["id"]))
            if current is None:
                return False
            current = dict(current)
            if any(
                current[file_key] and not bool(current[deleted_key])
                for _, file_key, deleted_key in files
            ):
                return False
            self.batches.update_batch(
                str(batch["id"]),
                cleanup_status="completed",
                cleanup_completed_at=utc_now(),
                cleanup_final_action=self._cleanup_action_for(current),
                cleanup_error_code=None,
                cleanup_error_message=None,
            )
            complete_cleanup()
            self._cleanup_local(current)
            return True

        if not any(batch[file_key] for _, file_key, _ in files):
            if str(batch.get("status")) in {"upload_unknown", "submission_unknown"}:
                self.batches.update_batch(
                    batch["id"],
                    cleanup_status="pending",
                    cleanup_error_code="BATCH-CLEANUP-OPERATOR-REQUIRED",
                    cleanup_error_message="未知遠端 side effect 尚無可驗證 File/Batch identity，不得宣告 cleanup not_required",
                )
                return False
            self.batches.update_batch(
                batch["id"],
                cleanup_status="not_required",
                cleanup_completed_at=utc_now(),
                cleanup_final_action=self._cleanup_action_for(batch),
            )
            complete_cleanup()
            self._cleanup_local(batch)
            return True
        pending = [entry for entry in files if batch[entry[1]] and not bool(batch[entry[2]])]
        if not pending:
            self.batches.update_batch(
                batch["id"],
                cleanup_status="completed",
                cleanup_completed_at=utc_now(),
                cleanup_final_action=self._cleanup_action_for(batch),
                cleanup_error_code=None,
                cleanup_error_message=None,
            )
            complete_cleanup()
            self._cleanup_local(batch)
            return True
        matched, code, message = self._cleanup_identity_matches(batch)
        if not matched:
            self._mark_cleanup_provider_unknown(str(batch["id"]), code, message)
            return False
        provider = None
        try:
            provider = self._provider(str(batch["provider_id"]), plan)
        except Exception as exc:
            self._mark_cleanup_provider_unknown(
                str(batch["id"]), str(getattr(exc, "code", "BATCH-CLEANUP-PROVIDER-001")), str(exc)
            )
            return False
        failed = False
        contended = False
        try:
            for file_kind, _file_key, _ in pending:
                owner = f"cleanup:{file_kind}:{uuid4()}"
                lease_until = self._side_effect_lease_until(str(batch["provider_id"]))
                claim = self.batches.claim_cleanup_file(batch["id"], file_kind, owner, lease_until)
                if claim is None:
                    # Another worker may own this file, or may have completed it
                    # after this worker's initial snapshot.  Neither is a cleanup
                    # failure and neither may regress a converging cleanup to
                    # partial.
                    contended = True
                    continue
                file_id, uncertain_delete = claim
                try:
                    if uncertain_delete:
                        retrieve_file = getattr(provider, "retrieve_file", None)
                        if callable(retrieve_file):
                            try:
                                retrieve_file(file_id)
                            except NotImplementedError:
                                # A provider may not expose File metadata.  DELETE is
                                # idempotent, so retry it and accept not-found below.
                                pass
                            except Exception as exc:
                                status = int(getattr(exc, "http_status", 0) or 0)
                                code = str(getattr(exc, "code", ""))
                                if status in {404, 410} or any(
                                    marker in code.casefold()
                                    for marker in ("not_found", "not-found", "expired")
                                ):
                                    if not self.batches.complete_cleanup_file(
                                        batch["id"], file_kind, owner
                                    ):
                                        contended = True
                                    continue
                                recorded = self.batches.fail_cleanup_file(
                                    batch["id"],
                                    owner,
                                    code or "BATCH-CLEANUP-RECONCILE",
                                    str(exc),
                                )
                                if recorded:
                                    failed = True
                                else:
                                    contended = True
                                continue
                    provider.delete_remote_file(file_id)
                except Exception as exc:
                    status = int(getattr(exc, "http_status", 0) or 0)
                    code = str(getattr(exc, "code", "BATCH-CLEANUP-001"))
                    # DELETE is idempotent from the lifecycle perspective:
                    # not-found and expired remote files are already cleaned.
                    if status not in {404, 410} and not any(
                        marker in code.casefold() for marker in ("not_found", "not-found", "expired")
                    ):
                        recorded = self.batches.fail_cleanup_file(
                            str(batch["id"]),
                            owner,
                            code,
                            str(exc),
                        )
                        if recorded:
                            failed = True
                        else:
                            contended = True
                        continue
                if not self.batches.complete_cleanup_file(str(batch["id"]), file_kind, owner):
                    contended = True
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        if complete_if_converged():
            return True
        if failed:
            current = dict(self.batches.get(str(batch["id"])) or batch)
            cleanup_changes: dict[str, Any] = {"cleanup_status": "partial"}
            if str(current["status"]) not in TERMINAL_BATCH_STATUSES:
                cleanup_changes["status"] = "cleanup_pending"
            self.batches.update_batch(str(batch["id"]), **cleanup_changes)
            return False
        if contended:
            return False
        self.batches.update_batch(
            str(batch["id"]),
            cleanup_status="completed",
            cleanup_completed_at=utc_now(),
            cleanup_final_action=self._cleanup_action_for(batch),
            cleanup_error_code=None,
            cleanup_error_message=None,
        )
        complete_cleanup()
        self._cleanup_local(batch)
        return True

    def _finish(self, batch_id: str) -> None:
        batch = self.batches.get(batch_id)
        if batch is None:
            return
        if str(batch["status"]) in TERMINAL_BATCH_STATUSES:
            # Cleanup may be retried after a terminal result has already been
            # recorded.  Never derive a new semantic from remote_status or
            # item counts and accidentally rewrite completed/failed/cancelled/
            # expired into another terminal state.
            return
        if str(batch["status"]) in {"upload_unknown", "submission_unknown"} or str(
            batch["remote_status"] or ""
        ) in {"upload_unknown", "submission_unknown"}:
            # Unknown side effects retain the item reservation and the parent
            # Job.  Only verified Recovery or explicit Abandon may leave this
            # state; never terminalize it as a local failure.
            self.batches.update_batch(batch_id, completed_at=None)
            return
        counts = self.batches.counts(batch_id)
        if any(
            int(counts.get(status, 0))
            for status in ("pending", "submitted", "upload_unknown", "submission_unknown")
        ):
            # A terminal marker without resolving these states would leave a
            # live reservation behind.  Local failure paths use
            # fail_local_batch; remote imports convert omissions to retry_pending.
            return
        imported = int(counts.get("imported", 0))
        failed = sum(
            int(counts.get(status, 0))
            for status in ("failed", "schema_invalid", "duplicate_custom_id", "unexpected_custom_id")
        )
        missing = int(counts.get("missing", 0) + counts.get("retry_pending", 0))
        stale = int(counts.get("stale", 0))
        current_status = str(batch["remote_status"] or "")
        reconciliation_error = bool(batch["reconciliation_error_code"])
        final_status = (
            "completed"
            if not failed and not missing and not stale and not reconciliation_error
            else "completed_with_errors"
        )
        if current_status in {"expired", "cancelled"}:
            final_status = current_status
        if current_status in {"failed", "abandoned"} and imported == 0:
            final_status = "failed"
        cleanup_status = str(batch["cleanup_status"] or "pending")
        self.batches.finalize_batch_result(
            batch_id,
            status=final_status,
            cleanup_final_action=(
                "complete" if str(batch["cleanup_final_action"] or "none") == "none" else None
            ),
            cleanup_status=cleanup_status if cleanup_status in {"completed", "not_required"} else None,
        )
        if batch["job_id"]:
            self.photos.refresh_dirty_libraries_for_job(str(batch["job_id"]))
        self._activity(
            "INFO" if final_status == "completed" else "WARNING",
            "batch_completed" if final_status.startswith("completed") else "batch_failed",
            "Batch lifecycle reached a terminal result",
            batch_id=batch_id,
            job_id=str(batch["job_id"] or ""),
            status=final_status,
        )

    def _fail_closed_frozen_plan(
        self, batch_id: str, batch: dict[str, Any], message: str
    ) -> dict[str, Any] | None:
        """Stop import and only clean through the exact persisted Provider identity."""

        batch = dict(batch)
        original_status = str(batch["status"])
        original_remote_status = batch.get("remote_status")
        original_completed_at = batch.get("completed_at")
        terminal = original_status in TERMINAL_BATCH_STATUSES

        def verify_terminal_semantic() -> None:
            if not terminal:
                return
            current = self.batches.get(batch_id)
            if current is None:
                return
            current = dict(current)
            if (
                str(current["status"]) != original_status
                or current.get("remote_status") != original_remote_status
                or current.get("completed_at") != original_completed_at
            ):
                raise BatchLifecycleError(
                    "Frozen Plan cleanup 不得改寫既有 terminal Batch semantic",
                    "BATCH-CLEANUP-TERMINAL-001",
                )

        has_files = any(batch[key] for key in ("input_file_id", "output_file_id", "error_file_id"))
        if not has_files:
            if terminal:
                self._hold_terminal_cleanup_for_invalid_plan(
                    batch,
                    error_code="BATCH-IMPORT-PLAN-001",
                    error_message=message,
                    cleanup_status="not_required",
                )
                verify_terminal_semantic()
                return None
            self.batches.finalize_batch_result(
                batch_id,
                status="failed",
                cleanup_final_action="fail",
                cleanup_status="not_required",
                error_code="BATCH-IMPORT-PLAN-001",
                error_message=message,
            )
            return None
        if terminal:
            self.batches.update_batch(
                batch_id,
                cleanup_status="pending",
                cleanup_error_code="BATCH-IMPORT-PLAN-001",
                cleanup_error_message=message[:1000],
            )
            try:
                plan = self._cleanup_plan(batch)
            except BatchLifecycleError as exc:
                self._mark_cleanup_provider_unknown(batch_id, exc.code, str(exc))
                verify_terminal_semantic()
                return None
            verify_terminal_semantic()
            return plan
        self.batches.update_batch(
            batch_id,
            status="cleanup_pending",
            remote_status="failed",
            cleanup_status="pending",
            cleanup_final_action="fail",
            reconciliation_error_code="BATCH-IMPORT-PLAN-001",
            reconciliation_error_message=message,
            completed_at=None,
        )
        try:
            return self._cleanup_plan(batch)
        except BatchLifecycleError as exc:
            self._mark_cleanup_provider_unknown(batch_id, exc.code, str(exc))
            return None

    @staticmethod
    def _terminal_import_mode(
        batch: Mapping[str, Any],
    ) -> Literal["noop", "cleanup_only"] | None:
        """Return the only safe import mode for a terminal Batch."""

        if str(batch["status"]) not in TERMINAL_BATCH_STATUSES:
            return None
        if str(batch["cleanup_status"] or "") in {"completed", "not_required"}:
            return "noop"
        return "cleanup_only"

    def import_batch(self, batch_id: str, *, cleanup_only: bool = False) -> dict[str, Any]:
        import_started = time.monotonic()
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        terminal_mode = self._terminal_import_mode(batch)
        if terminal_mode == "noop":
            return {"batch_id": batch_id, "already_imported": True}
        if terminal_mode == "cleanup_only":
            cleanup_only = True
        self._activity(
            "INFO",
            "batch_result_ingest",
            "Batch result ingest started",
            batch_id=batch_id,
            job_id=str(batch["job_id"] or ""),
            status=str(batch["status"] or ""),
        )
        plan_row = self.jobs.get(str(batch["job_id"])) if batch["job_id"] else None
        plan_error: str | None = None
        try:
            plan = json.loads(str(plan_row["analysis_spec_json"] or "{}")) if plan_row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            plan = None
            plan_error = "Frozen Analysis Plan JSON 無法解析；不匯入不完整結果"
        if plan_error is not None:
            plan = self._fail_closed_frozen_plan(batch_id, dict(batch), plan_error)
            cleanup_only = True
        elif not isinstance(plan, dict) or not plan.get("provider_route"):
            plan = self._fail_closed_frozen_plan(
                batch_id, dict(batch), "Frozen Analysis Plan JSON 缺少有效 provider route"
            )
            cleanup_only = True
        if cleanup_only or str(batch["status"]) == "cleanup_pending":
            if isinstance(plan, dict):
                current = self.batches.get(batch_id)
                self._cleanup_remote(dict(current) if current is not None else dict(batch), plan)
            return {"batch_id": batch_id, "cleanup_only": True}
        assert isinstance(plan, dict)
        self.batches.update_batch(batch_id, status="importing")
        provider = self._provider(str(batch["provider_id"]), plan)
        try:
            output_path = Path(
                str(batch["local_output_path"] or (self.batch_root / batch_id / "output.jsonl"))
            )
            error_path = Path(str(batch["local_error_path"] or (self.batch_root / batch_id / "error.jsonl")))
            if batch["output_file_id"] and not output_path.is_file():
                provider.download_file_content(str(batch["output_file_id"]), output_path)
                self.batches.update_batch(batch_id, local_output_path=str(output_path))
            if batch["error_file_id"] and not error_path.is_file():
                provider.download_file_content(str(batch["error_file_id"]), error_path)
                self.batches.update_batch(batch_id, local_error_path=str(error_path))
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        provider = self._provider(str(batch["provider_id"]), plan)
        records: dict[str, tuple[str, dict[str, Any], dict[str, Any] | None]] = {}
        successes: set[str] = set()
        errors: set[str] = set()
        try:
            current_batch = self.batches.get(batch_id)
            self._read_results(
                Path(str(current_batch["local_output_path"])) if current_batch["local_output_path"] else None,
                current_batch,
                is_error=False,
                records=records,
            )
            current_batch = self.batches.get(batch_id)
            self._read_results(
                Path(str(current_batch["local_error_path"])) if current_batch["local_error_path"] else None,
                current_batch,
                is_error=True,
                records=records,
            )
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        expected: dict[str, dict[str, Any]] = {
            str(item["custom_id"]): item for item in self.batches.items(batch_id)
        }
        try:
            _current_plan, current_analysis_fingerprint, _current_model = self._plan()
        except Exception:
            current_analysis_fingerprint = None
        unknown = set(records) - set(expected)
        if unknown:
            self.batches.update_batch(
                batch_id,
                reconciliation_error_code="unexpected_custom_id",
                reconciliation_error_message=f"結果檔含 {len(unknown)} 個未預期 custom_id",
            )
        for custom_id, (kind, record, body) in records.items():
            expected_item: dict[str, Any] | None = expected.get(custom_id)
            if expected_item is None:
                continue
            if kind == "duplicate":
                self._mark_item_error(
                    expected_item,
                    "duplicate_custom_id",
                    "同一 custom_id 出現在多個結果行或成功／錯誤檔",
                    "duplicate_custom_id",
                    job_id=str(batch["job_id"]),
                )
                errors.add(custom_id)
            elif kind == "error":
                error = record.get("error") or {}
                self._mark_item_error(
                    expected_item,
                    str(error.get("code") or "BATCH-HTTP-ERROR"),
                    str(error.get("message") or "遠端 Batch 項目失敗"),
                    "failed",
                    job_id=str(batch["job_id"]),
                )
                errors.add(custom_id)
            elif kind == "schema_invalid":
                self._mark_item_error(
                    expected_item,
                    "BATCH-RESPONSE-BODY",
                    "Batch Response Body 不是 JSON Object",
                    "schema_invalid",
                    job_id=str(batch["job_id"]),
                )
                errors.add(custom_id)
            elif kind == "success" and body is not None:
                outcome = self._import_success(
                    batch,
                    expected_item,
                    record,
                    body,
                    plan,
                    provider,
                    current_analysis_fingerprint,
                )
                if outcome in {"imported", "already_imported"}:
                    successes.add(custom_id)
                elif outcome not in {"pending", "submitted", "retry_pending"}:
                    errors.add(custom_id)
        seen = set(records)
        for custom_id, item in expected.items():
            if custom_id in seen:
                continue
            if str(item["status"]) in {
                "imported",
                "failed",
                "schema_invalid",
                "stale",
                "duplicate_custom_id",
            }:
                continue
            self.batches.update_item(
                str(item["id"]),
                status="retry_pending",
                error_code="missing_result",
                error_message="Output/Error File 未包含此 custom_id",
            )
            if item.get("job_item_id"):
                self.jobs.fail_batch_item(
                    str(batch["job_id"]),
                    str(item["job_item_id"]),
                    "missing_result",
                    "Output/Error File 未包含此 custom_id",
                )
        current = self.batches.get(batch_id)
        if current is not None:
            self._cleanup_remote(current, plan)
        current = self.batches.get(batch_id)
        if current is not None and str(current["cleanup_status"]) == "completed":
            self._finish(batch_id)
        result = {
            "batch_id": batch_id,
            "success": len(successes),
            "errors": len(errors),
            "missing": len(set(expected) - seen),
        }
        log_event(
            LOGGER,
            logging.INFO if not errors else logging.WARNING,
            "Batch result ingest completed",
            event="batch_result_ingest_completed",
            batch_id=batch_id,
            job_id=str(batch["job_id"] or ""),
            duration_ms=int((time.monotonic() - import_started) * 1000),
            details={
                "success": result["success"],
                "errors": result["errors"],
                "missing": result["missing"],
            },
        )
        return result

    def get_detail(self, batch_id: str) -> dict[str, Any] | None:
        batch = self.batches.get(batch_id)
        if batch is None:
            return None
        result = dict(batch)
        result["items_by_status"] = self.batches.counts(batch_id)
        result["items"] = self.batches.items(batch_id)
        input_path = Path(str(batch["local_input_path"])) if batch["local_input_path"] else None
        result["actual_jsonl_bytes"] = input_path.stat().st_size if input_path and input_path.is_file() else 0
        shard_rows = self.batches.list(limit=500)
        result["shard_sizes"] = []
        for shard in shard_rows:
            if str(shard.get("job_id") or "") != str(batch["job_id"] or ""):
                continue
            shard_path = Path(str(shard["local_input_path"])) if shard.get("local_input_path") else None
            result["shard_sizes"].append(
                {
                    "batch_id": str(shard["id"]),
                    "bytes": shard_path.stat().st_size if shard_path and shard_path.is_file() else 0,
                }
            )
        imported = int(batch["imported_items"] or 0)
        result["average_cost"] = float(batch["actual_cost"] or 0) / imported if imported else 0.0
        result["per_thousand_cost"] = result["average_cost"] * 1000
        result["schema_success_rate"] = (imported / int(batch["total_items"] or 1)) * 100
        with self.database.session() as connection:
            result["eligible_missing_count"] = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM photos
                    WHERE lifecycle_status='active' AND eligible=1 AND COALESCE(never_upload,0)=0
                      AND COALESCE(exclusion_status,'')!='manually_excluded'
                      AND NOT EXISTS (
                          SELECT 1 FROM photo_analysis a
                          WHERE a.photo_id=photos.id AND a.analysis_fingerprint=?
                            AND COALESCE(a.schema_kind,'basic')='full'
                      )
                    """,
                    (str(batch["analysis_fingerprint"]),),
                ).fetchone()[0]
            )
        result["full_library_estimated_cost"] = result["average_cost"] * result["eligible_missing_count"]
        return result
