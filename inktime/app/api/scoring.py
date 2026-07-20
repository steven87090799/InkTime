from __future__ import annotations

from pathlib import Path
from statistics import median
import tempfile

from flask import Blueprint, abort, current_app, g, render_template, request
from PIL import UnidentifiedImageError

from inktime.app.domain.analysis import AnalysisValidationError
from inktime.app.domain.analysis.scoring import (
    DISTINCTIVE_SCORING_RULES,
    calculate_distinguishing_score,
    prepare_score_distribution,
    score_band,
)
from inktime.app.providers.openai_compatible import ProviderHTTPError
from inktime.app.services.budgets import BudgetExceeded
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("scoring", __name__)
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
MAX_TEST_PHOTO_BYTES = 25 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


@bp.get("/scoring")
@login_required
def scoring_page():
    repository = current_app.extensions["inktime_scoring_repository"]
    population = current_app.extensions["inktime_photo_repository"].score_population()
    distribution = {
        "count": len(population),
        "minimum": round(min(population), 1) if population else None,
        "maximum": round(max(population), 1) if population else None,
        "median": round(median(population), 1) if population else None,
        "spread": round(max(population) - min(population), 1) if population else None,
        "calibration_ready": len(population) >= 5 and len(set(population)) >= 3,
    }
    return render_template(
        "scoring.html",
        current_profile=repository.current(),
        versions=repository.list(),
        provider_count=sum(
            1
            for provider in current_app.extensions["inktime_provider_repository"].list()
            if provider["enabled"]
        ),
        distribution=distribution,
        recommended_rules=DISTINCTIVE_SCORING_RULES,
    )


@bp.post("/api/v1/scoring/profiles")
@administrator_required
def create_profile():
    payload = request.get_json(silent=True) or {}
    try:
        profile = current_app.extensions["inktime_scoring_repository"].create(
            name=str(payload.get("name", "")),
            rules=str(payload.get("rules", "")),
            weights={
                "memory": float(payload.get("memory_weight", 0)),
                "beauty": float(payload.get("beauty_weight", 0)),
                "technical_quality": float(payload.get("technical_weight", 0)),
                "emotion": float(payload.get("emotion_weight", 0)),
            },
            favorite_bonus=float(payload.get("favorite_bonus", 0)),
            created_by=str(g.user["id"]),
            source_ip=request.remote_addr or "unknown",
        )
    except (TypeError, ValueError) as exc:
        abort(400, description=f"SET-002 {exc}")
    return {"id": profile["id"], "name": profile["name"]}, 201


@bp.post("/api/v1/scoring/profiles/<version_id>/restore")
@administrator_required
def restore_profile(version_id: str):
    try:
        profile = current_app.extensions["inktime_scoring_repository"].restore(
            version_id,
            created_by=str(g.user["id"]),
            source_ip=request.remote_addr or "unknown",
        )
    except KeyError:
        abort(404)
    return {"id": profile["id"], "name": profile["name"]}, 201


@bp.post("/api/v1/scoring/test")
@administrator_required
def test_scoring():
    uploaded = request.files.get("photo")
    if uploaded is None or not uploaded.filename:
        abort(400, description="IMG-002 請選擇測試照片")
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        abort(400, description="IMG-002 測試照片格式不支援")
    with tempfile.TemporaryDirectory(prefix="inktime-scoring-") as directory:
        source = Path(directory) / f"source{suffix}"
        normalized = Path(directory) / "normalized.jpg"
        size = 0
        with source.open("wb") as destination:
            while chunk := uploaded.stream.read(UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_TEST_PHOTO_BYTES:
                    abort(413, description="IMG-002 測試照片不可超過 25 MiB")
                destination.write(chunk)
        try:
            current_app.extensions["inktime_scoring_lab_service"].normalize_image(
                source, normalized
            )
            result = current_app.extensions["inktime_scoring_lab_service"].analyze(normalized)
        except (UnidentifiedImageError, OSError):
            abort(400, description="IMG-002 無法解碼測試照片")
        except BudgetExceeded as exc:
            abort(409, description=f"{exc.code} {exc}")
        except ProviderHTTPError as exc:
            abort(502, description=f"{exc.code} {exc}")
        except AnalysisValidationError as exc:
            abort(422, description=f"VLM-004 {exc}")
        except ValueError as exc:
            description = str(exc)
            abort(
                400,
                description=(
                    description if "-" in description[:12] else f"VLM-008 {description}"
                ),
            )
    raw_score = float(result["ranking_score"])
    calibrated, percentile = calculate_distinguishing_score(
        raw_score,
        prepare_score_distribution(
            current_app.extensions["inktime_photo_repository"].score_population()
        ),
    )
    result["distinguishing_score"] = calibrated
    result["ranking_percentile"] = percentile
    result["score_band"] = score_band(percentile, calibrated)
    return result
