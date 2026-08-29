from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
from uuid import uuid4

from PIL import Image, ImageOps

from inktime.app.domain.analysis import AnalysisValidationError, validate_analysis_result
from inktime.app.domain.analysis.scoring import calculate_ranking_score
from inktime.app.repositories.scoring import ScoringProfileRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.providers.base import VisionAttemptState
from inktime.app.services.budgets import BudgetService
from inktime.app.services.providers import ProviderService
from inktime.app.services.usage_tracking import record_failed_unknown_usage


MAX_TEST_PHOTO_PIXELS = 40_000_000


class ScoringLabService:
    def __init__(
        self,
        providers: ProviderService,
        profiles: ScoringProfileRepository,
        settings: SettingsRepository,
        usage: UsageRepository,
        budgets: BudgetService,
    ) -> None:
        self.providers = providers
        self.profiles = profiles
        self.settings = settings
        self.usage = usage
        self.budgets = budgets

    @staticmethod
    def normalize_image(source: Path, destination: Path) -> None:
        if source.suffix.lower() in {".heic", ".heif"}:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        with Image.open(source) as opened:
            if opened.width * opened.height > MAX_TEST_PHOTO_PIXELS:
                raise ValueError("IMG-002 測試照片像素不可超過 4000 萬")
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            image.save(destination, "JPEG", quality=90, optimize=True)

    def analyze(self, image_path: Path) -> dict:
        provider = self.providers.build_router()
        if provider is None:
            raise ValueError("VLM-008 尚未設定可用 Provider")
        try:
            return self._analyze_with_provider(image_path, provider)
        finally:
            close_provider = getattr(provider, "close", None)
            if callable(close_provider):
                close_provider()

    def _analyze_with_provider(self, image_path: Path, provider) -> dict:
        self.budgets.assert_request_allowed(None, None)
        profile = self.profiles.current()
        model = str(self.settings.get("model.high_model", "gpt-4o"))
        max_tokens = max(256, min(int(self.settings.get("budget.max_tokens", 8000)), 2048))
        provider_request_context_id = f"scoring_test|{uuid4()}"
        selected_provider = provider
        provider_id = ""
        provider_name = provider.name
        attempt_summary: list[dict] = []
        vision_request_state = VisionAttemptState()

        def refresh_provider_identity() -> None:
            nonlocal selected_provider, provider_id, provider_name, model
            selected = getattr(getattr(provider, "_local", None), "channel", None)
            selected_provider = selected.provider if selected is not None else provider
            provider_id = str(getattr(selected_provider, "provider_id", selected_provider.name))
            provider_name = str(getattr(selected_provider, "name", provider.name))
            configured_model = str(getattr(selected, "model", "") or "").strip()
            if configured_model:
                model = configured_model

        def provider_request_metrics() -> dict:
            return dict(getattr(selected_provider, "last_request_metrics", {}) or {})

        def record_attempt(
            response,
            *,
            request_type: str,
            started_at: str,
            started_perf: float,
            retry_count: int,
            image_bytes: bool,
        ) -> dict:
            usage = response.usage
            recorded_model = str(response.served_model or model)
            estimated = provider.estimate_cost(recorded_model, usage)
            reported = usage.provider_reported_cost
            source = "provider_reported" if reported is not None else "estimated" if estimated is not None else "unknown"
            metrics = dict(response.request_metrics or getattr(selected_provider, "last_request_metrics", {}) or {})
            if image_bytes:
                # Some compatible/fake providers do not expose transport
                # metrics.  The Vision attempt still carried the image, so
                # preserve a positive local byte measurement rather than
                # silently recording it as a text-only request.
                reported_image_bytes = metrics.get("image_bytes", 0)
                try:
                    reported_image_bytes = int(reported_image_bytes)
                except (TypeError, ValueError):
                    reported_image_bytes = 0
                metrics["image_bytes"] = (
                    reported_image_bytes
                    if reported_image_bytes > 0
                    else image_path.stat().st_size
                )
            else:
                metrics["image_bytes"] = 0
            effective = (
                max(0.0, float(reported))
                if reported is not None
                else max(0.0, float(estimated))
                if estimated is not None
                else 0.0
            )
            self.usage.record(
                provider=provider_name,
                provider_id=provider_id,
                model=recorded_model,
                job_id=None,
                photo_id=None,
                request_type=request_type,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                estimated_cost=estimated,
                actual_cost=max(0.0, float(reported)) if reported is not None else None,
                started_at=started_at,
                latency_ms=int((time.perf_counter() - started_perf) * 1000),
                status="completed",
                retry_count=retry_count,
                request_id=response.request_id,
                reasoning_tokens=usage.reasoning_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_source=source,
                prompt_chars=metrics.get("prompt_chars", 0),
                schema_chars=metrics.get("schema_chars", 0),
                request_body_bytes=metrics.get("request_body_bytes", 0),
                image_bytes=metrics.get("image_bytes", 0),
            )
            result = {
                "request_type": request_type,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_tokens": usage.cached_tokens,
                "estimated_cost": estimated,
                "actual_cost": reported,
                "effective_cost": effective,
                "cost_source": source,
                "latency_ms": int((time.perf_counter() - started_perf) * 1000),
                "image_bytes": metrics.get("image_bytes", 0),
            }
            attempt_summary.append(result)
            return result

        def record_failed_attempt(
            *,
            request_type: str,
            model_name: str,
            started_at: str,
            started_perf: float,
            retry_count: int,
            image_request: bool,
            error: Exception,
        ) -> None:
            refresh_provider_identity()
            metrics = provider_request_metrics()
            if image_request:
                try:
                    reported_image_bytes = int(metrics.get("image_bytes", 0))
                except (TypeError, ValueError):
                    reported_image_bytes = 0
                metrics["image_bytes"] = (
                    reported_image_bytes
                    if reported_image_bytes > 0
                    else image_path.stat().st_size
                )
            else:
                metrics["image_bytes"] = 0
            error_code = str(getattr(error, "code", "") or error.__class__.__name__)
            record_failed_unknown_usage(
                self.usage,
                provider=selected_provider,
                model=model_name,
                job_id=None,
                photo_id=None,
                request_type=request_type,
                started_at=started_at,
                started_perf=started_perf,
                error=error,
                request_metrics=metrics,
                retry_count=retry_count,
                image_bytes=metrics.get("image_bytes", 0),
                error_code=error_code,
            )
            attempt_summary.append(
                {
                    "request_type": request_type,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "estimated_cost": None,
                    "actual_cost": None,
                    "effective_cost": 0.0,
                    "cost_source": "unknown",
                    "latency_ms": int((time.perf_counter() - started_perf) * 1000),
                    "image_bytes": metrics.get("image_bytes", 0),
                    "status": "failed",
                    "error_code": error_code,
                }
            )

        vision_started_at = datetime.now(timezone.utc).isoformat()
        vision_started_perf = time.perf_counter()
        try:
            response = provider.analyze(
                image_path=image_path,
                model=model,
                detail="high",
                stage="scoring_test",
                max_tokens=max_tokens,
                vision_attempt=vision_request_state,
                provider_request_context_id=provider_request_context_id,
            )
        except Exception as error:
            refresh_provider_identity()
            request_started = bool(
                vision_request_state.vision_started
                or getattr(error, "vision_started", False)
                or getattr(error, "request_started", False)
                or getattr(error, "ambiguous", False)
            )
            if request_started:
                record_failed_attempt(
                    request_type="scoring_test_vision",
                    model_name=model,
                    started_at=vision_started_at,
                    started_perf=vision_started_perf,
                    retry_count=0,
                    image_request=True,
                    error=error,
                )
            raise
        refresh_provider_identity()
        vision_summary = record_attempt(
            response,
            request_type="scoring_test_vision",
            started_at=vision_started_at,
            started_perf=vision_started_perf,
            retry_count=0,
            image_bytes=True,
        )
        try:
            result = validate_analysis_result(response.content)
        except AnalysisValidationError as error:
            repair_started_at = datetime.now(timezone.utc).isoformat()
            repair_started_perf = time.perf_counter()
            try:
                repaired = provider.repair_json(
                    invalid_content=response.content,
                    validation_error=str(error),
                    model=model,
                    max_tokens=max_tokens,
                    stage="scoring_test",
                    provider_request_context_id=provider_request_context_id,
                )
            except Exception as repair_error:
                request_started = bool(
                    getattr(repair_error, "request_started", False)
                    or getattr(repair_error, "vision_started", False)
                    or getattr(repair_error, "ambiguous", False)
                )
                if request_started:
                    record_failed_attempt(
                        request_type="scoring_test_repair",
                        model_name=model,
                        started_at=repair_started_at,
                        started_perf=repair_started_perf,
                        retry_count=1,
                        image_request=False,
                        error=repair_error,
                    )
                raise
            record_attempt(
                repaired,
                request_type="scoring_test_repair",
                started_at=repair_started_at,
                started_perf=repair_started_perf,
                retry_count=1,
                image_bytes=False,
            )
            result = validate_analysis_result(repaired.content)
        total_cost = sum(float(item["effective_cost"]) for item in attempt_summary)
        unknown_count = sum(item["cost_source"] == "unknown" for item in attempt_summary)
        cost_source = (
            "incomplete"
            if unknown_count
            else "provider_reported"
            if all(item["cost_source"] == "provider_reported" for item in attempt_summary)
            else "estimated"
        )
        total_input = sum(int(item["input_tokens"]) for item in attempt_summary)
        total_output = sum(int(item["output_tokens"]) for item in attempt_summary)
        total_cached = sum(int(item["cached_tokens"]) for item in attempt_summary)
        weights = {
            "memory": float(profile["memory_weight"]),
            "beauty": float(profile["beauty_weight"]),
            "technical_quality": float(profile["technical_weight"]),
            "emotion": float(profile["emotion_weight"]),
        }
        return {
            "analysis": result,
            "ranking_score": calculate_ranking_score(
                result,
                weights,
                favorite=False,
                favorite_bonus=float(profile["favorite_bonus"]),
            ),
            "profile": {"id": profile["id"], "name": profile["name"]},
            "usage": {
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cached_tokens": total_cached,
                "cost": total_cost,
                "cost_source": cost_source,
                "cost_complete": unknown_count == 0,
                "unknown_cost_count": unknown_count,
                "vision_cost": vision_summary["effective_cost"],
                "repair_cost": next(
                    (item["effective_cost"] for item in attempt_summary if item["request_type"] == "scoring_test_repair"),
                    0.0,
                ),
                "attempts": attempt_summary,
                "latency_ms": sum(int(item["latency_ms"]) for item in attempt_summary),
                "provider": provider_name,
                "provider_id": provider_id,
                "model": model,
            },
        }
