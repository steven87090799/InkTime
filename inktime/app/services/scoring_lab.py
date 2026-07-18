from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

from PIL import Image, ImageOps

from inktime.app.domain.analysis import AnalysisValidationError, validate_analysis_result
from inktime.app.domain.analysis.scoring import calculate_ranking_score
from inktime.app.providers.base import Usage
from inktime.app.repositories.scoring import ScoringProfileRepository
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.repositories.usage import UsageRepository
from inktime.app.services.budgets import BudgetService
from inktime.app.services.providers import ProviderService


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
        self.budgets.assert_request_allowed(None, None)
        profile = self.profiles.current()
        model = str(self.settings.get("model.high_model", "gpt-4o"))
        max_tokens = int(self.settings.get("budget.max_tokens", 8000))
        started_at = datetime.now(timezone.utc).isoformat()
        started_perf = time.perf_counter()
        response = provider.analyze(
            image_path=image_path,
            model=model,
            detail="high",
            stage="scoring_test",
            max_tokens=max_tokens,
        )
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cached_tokens = response.usage.cached_tokens
        retry_count = 0
        try:
            result = validate_analysis_result(response.content)
        except AnalysisValidationError as error:
            repaired = provider.repair_json(
                invalid_content=response.content,
                validation_error=str(error),
                model=model,
                max_tokens=max_tokens,
            )
            input_tokens += repaired.usage.input_tokens
            output_tokens += repaired.usage.output_tokens
            cached_tokens += repaired.usage.cached_tokens
            retry_count = 1
            result = validate_analysis_result(repaired.content)

        cost = provider.estimate_cost(
            model,
            Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
            ),
        )
        latency_ms = int((time.perf_counter() - started_perf) * 1000)
        self.usage.record(
            provider=provider.name,
            model=model,
            job_id=None,
            photo_id=None,
            request_type="scoring_test",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            estimated_cost=cost,
            actual_cost=cost,
            started_at=started_at,
            latency_ms=latency_ms,
            status="completed",
            retry_count=retry_count,
        )
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
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
                "cost": cost,
                "latency_ms": latency_ms,
                "provider": provider.name,
                "model": model,
            },
        }
