from __future__ import annotations

from inktime.app.domain.photos.formats import SUPPORTED_IMAGE_EXTENSIONS

from pathlib import Path
import json
from statistics import median
import tempfile

from flask import Blueprint, abort, current_app, g, render_template, request
from PIL import UnidentifiedImageError

from inktime.app.core.json_values import JsonScalarError, json_object_payload
from inktime.app.domain.analysis import AnalysisValidationError
from inktime.app.domain.analysis.scoring import (
    DISTINCTIVE_SCORING_RULES,
    DEFAULT_RANKING_WEIGHTS,
    score_band,
    SPECIAL_BONUSES,
    ranking_components,
    normalize_scoring_rules,
)
from inktime.app.domain.analysis.schema import json_schema_for_stage
from inktime.app.providers.config import effective_provider_kind
from inktime.app.providers.openai_compatible import (
    ProviderHTTPError,
    ANALYSIS_USER_PROMPT,
    JSON_REPAIR_PROMPT,
    analysis_prompt_sections,
    analysis_response_format,
    analysis_system_prompt,
)
from inktime.app.services.budgets import BudgetExceeded
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("scoring", __name__)

ALLOWED_IMAGE_SUFFIXES = SUPPORTED_IMAGE_EXTENSIONS
MAX_TEST_PHOTO_BYTES = 25 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


def _prompt_preview(rules: str | None = None) -> dict:
    """Inspect current settings only; never create a client, upload or call a model."""
    scope = request.args.get("scope", "analysis")
    if scope not in {"analysis", "scoring_test"}:
        abort(400, description="SET-002 不支援的提示詞預覽範圍")
    settings = current_app.extensions["inktime_settings_repository"]
    profile = current_app.extensions["inktime_scoring_repository"].current()
    plan = current_app.extensions["inktime_analysis_service"].build_plan(
        strategy="single", provider_route=[], scoring_profile=profile,
    )
    # The existing scoring lab builds its router without advanced caption controls.
    controls = plan["caption_controls"] if scope == "analysis" else None
    saved_rules = str(plan["scoring_rules"])
    selected_rules = saved_rules if rules is None else rules
    prompt = analysis_system_prompt(selected_rules, controls)
    saved_prompt = analysis_system_prompt(saved_rules, controls)
    schema = json_schema_for_stage("single", caption_controls=controls)
    providers = []
    provider_service = current_app.extensions["inktime_provider_service"]
    usable_ids = (
        {str(item["provider_id"]) for item in provider_service.usable_route_snapshot()}
        if scope == "analysis"
        else None
    )
    for row in current_app.extensions["inktime_provider_repository"].list():
        if not row["enabled"] or (usable_ids is not None and str(row["id"]) not in usable_ids):
            continue
        response_format = analysis_response_format(
            effective_provider_kind(str(row.get("kind") or "openai_compatible"), str(row["base_url"])),
            "single", supports_json_schema=bool(row["supports_json_schema"]), caption_controls=controls,
        )
        providers.append({
            "name": row["name"],
            "model": row.get("model") or (
                plan["model"] if scope == "analysis" else settings.get("model.high_model", "gpt-4o")
            ),
            "response_format": response_format,
            "schema_chars": len(json.dumps(response_format, ensure_ascii=False)) if response_format else 0,
        })
    return {
        "scope": scope,
        "rules": selected_rules,
        "profile_matches_settings": normalize_scoring_rules(profile["rules"]) == saved_rules.strip(),
        "sections": analysis_prompt_sections(selected_rules, controls),
        "system_prompt": prompt,
        "user_prompt": ANALYSIS_USER_PROMPT,
        "repair_prompt": JSON_REPAIR_PROMPT,
        "schema": schema,
        "fields": [{"name": name, "constraints": json.dumps(spec, ensure_ascii=False)}
                   for name, spec in schema["schema"]["properties"].items()],
        "providers": providers,
        "prompt_chars": len(prompt),
        "rules_chars": len(selected_rules.strip()),
        "saved_prompt_chars": len(saved_prompt),
        "char_delta": len(prompt) - len(saved_prompt),
        "is_draft": rules is not None,
        "weights": DEFAULT_RANKING_WEIGHTS,
        "special_bonuses": SPECIAL_BONUSES,
        "example": ranking_components({
            "memory_score": 80, "visual_score": 60, "local_quality_score": 80, "special_level": 2,
        }),
    }


def _editable_payload(*, preview: bool = False) -> dict:
    payload = json_object_payload(request, maximum_bytes=64 * 1024, error_prefix="SET-002")
    allowed = {"rules"} if preview else {"name", "rules"}
    if set(payload) - allowed:
        abort(400, description="SET-002 只能修改版本名稱與評分參考；輸出契約與排序權重已鎖定")
    try:
        payload["rules"] = normalize_scoring_rules(payload.get("rules"))
        if not preview and not isinstance(payload.get("name"), str):
            raise ValueError("版本名稱必須是文字")
    except ValueError as exc:
        abort(400, description=f"SET-002 {exc}")
    return payload


@bp.get("/api/v1/scoring/prompt")
@login_required
def current_prompt():
    return _prompt_preview()


@bp.post("/api/v1/scoring/prompt/preview")
@administrator_required
def preview_prompt():
    return _prompt_preview(_editable_payload(preview=True)["rules"])


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
        prompt_preview=_prompt_preview(),
    )


@bp.post("/api/v1/scoring/profiles")
@administrator_required
def create_profile():
    payload = _editable_payload()
    try:
        profile = current_app.extensions["inktime_scoring_repository"].create(
            name=str(payload.get("name", "")),
            rules=str(payload.get("rules", "")),
            weights=dict(DEFAULT_RANKING_WEIGHTS),
            favorite_bonus=1,
            created_by=str(g.user["id"]),
            source_ip=request.remote_addr or "unknown",
        )
    except (JsonScalarError, TypeError, ValueError) as exc:
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
    except ValueError as exc:
        abort(400, description=f"SET-002 {exc}")
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
            current_app.extensions["inktime_scoring_lab_service"].normalize_image(source, normalized)
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
                description=(description if "-" in description[:12] else f"VLM-008 {description}"),
            )
    raw_score = float(result["ranking_score"])
    result["score_band"] = score_band(raw_score)
    return result
