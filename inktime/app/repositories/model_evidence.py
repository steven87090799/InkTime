"""Permanent model inputs/outputs, separate from bounded diagnostic traces.

Headers and credentials are never stored. Exact image payloads are content
addressed under /data/model-inputs; original NAS photos remain read-only.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from inktime.app.db import Database
from inktime.app.domain.analysis import SCHEMA_VERSION
from inktime.app.domain.analysis.json_repair import extract_json_value
from inktime.app.domain.analysis.schema import validate_model_response
from inktime.app.providers.base import (
    IncompleteProviderResponse,
    ProviderResponse,
    assert_complete_response,
    deserialize_response,
)


class ModelEvidenceNotSavedError(RuntimeError):
    not_sent = True


class ModelResponseNotSavedError(RuntimeError):
    code = "VLM-EVIDENCE-RESPONSE"
    ambiguous = True
    request_started = True
    vision_started = True


class ModelReceiptReadError(ValueError):
    """The stored transport record cannot be decoded."""


@dataclass(frozen=True)
class SavedPhotoResponse:
    context: dict
    request: dict
    plan: dict
    plan_json: str | None
    response: ProviderResponse
    result: dict


def read_saved_photo_response(saved: dict) -> SavedPhotoResponse:
    """Validate the original response against its original analysis contract."""
    from inktime.app.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError
    from inktime.app.providers.openai_responses import responses_to_completion

    try:
        # Older approval records append an audit note after the original JSON.
        first_note = str(saved["resolution_note"] or "{}").split("\n", 1)[0]
        context = json.loads(first_note if first_note.startswith("{") else "{}")
        request = json.loads(saved["request_json"] or "{}")
        if not isinstance(context, dict) or not isinstance(request, dict):
            raise ValueError("invalid paid operation evidence")
        plan_json = context.get("analysis_spec_json") or saved["original_job_plan"]
        plan = json.loads(plan_json or "{}")
        if not isinstance(plan, dict):
            raise ValueError("invalid paid analysis plan")
        if saved["response_json"]:
            response = deserialize_response(saved["response_json"])
        else:
            payload = json.loads(saved["transport_response"] or "null")
            status = context.get("transport_status")
            if (status is not None and not 200 <= status < 300) or not isinstance(payload, dict) or payload.get("error"):
                raise ValueError("not a successful model response")
            if "input" in request or "output" in payload:
                payload = responses_to_completion(payload)
            response = OpenAICompatibleProvider.decode_completion(payload, request_id=context.get("transport_request_id"))
    except (ProviderHTTPError, KeyError, TypeError, ValueError) as exc:
        raise ModelReceiptReadError("無法核對已保存的模型請求") from exc

    assert_complete_response(response)
    payload = extract_json_value(response.content)
    schema_version = context.get("schema_version") or plan.get("schema_version")
    if schema_version is None and isinstance(payload, dict):
        schema_version = payload.get("schema_version")
    result = validate_model_response(
        payload if isinstance(payload, dict) else response.content,
        caption_controls=context.get("caption_controls") or plan.get("caption_controls"),
        schema_version=int(schema_version or SCHEMA_VERSION),
    )
    return SavedPhotoResponse(context, request, plan, plan_json, response, result)


def has_valid_saved_photo_response(saved: dict) -> bool:
    if not (saved["response_json"] or saved["transport_response"]):
        return False
    try:
        read_saved_photo_response(saved)
    except (ValueError, TypeError, IncompleteProviderResponse):
        return False
    return True


def _archive_image(database: Database, payload: bytes, mime: str) -> dict:
    digest = sha256(payload).hexdigest()
    directory = database.path.parent / "model-inputs" / digest[:2]
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / digest
    if not destination.is_file() or sha256(destination.read_bytes()).hexdigest() != digest:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".input-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            temporary = None
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {"sha256": digest, "bytes": len(payload), "mime": mime,
            "path": destination.relative_to(database.path.parent).as_posix()}


def save_model_request(database: Database, operation_id: str, request: dict) -> None:
    """Commit the complete reconstructable request before transport may start."""
    def archive(value):
        if isinstance(value, dict):
            return {str(key): archive(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [archive(item) for item in value]
        if isinstance(value, Path):
            return {"_inktime_image": _archive_image(database, value.read_bytes(), "image/jpeg")}
        if isinstance(value, str) and value.startswith("data:image/") and ";base64," in value:
            header, encoded = value.split(",", 1)
            return {"_inktime_image": _archive_image(database, base64.b64decode(encoded, validate=True),
                                                    header[5:].split(";", 1)[0])}
        return value

    try:
        manifest = json.dumps(archive(request), ensure_ascii=False, separators=(",", ":"))
        now = datetime.now(timezone.utc).isoformat()
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO model_request_evidence(operation_id,request_json,created_at,updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET "
                "request_json=excluded.request_json,updated_at=excluded.updated_at",
                (operation_id, manifest, now, now),
            )
    except Exception as exc:
        raise ModelEvidenceNotSavedError("模型輸入證據無法永久保存；請求未送出") from exc


def save_model_transport_response(
    database: Database, operation_id: str, raw: str, *,
    http_status: int | None = None, request_id: str | None = None,
) -> None:
    # An I/O failure here is deliberately not labelled not_sent. The durable
    # started operation must fence retries if a response cannot be committed.
    try:
        with database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE model_request_evidence SET response_json=?,updated_at=? WHERE operation_id=?",
                (raw, datetime.now(timezone.utc).isoformat(), operation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Missing durable model request")
            if http_status is not None:
                connection.execute(
                    """
                    UPDATE billable_operations SET resolution_note=json_set(
                        CASE WHEN json_valid(resolution_note) THEN resolution_note ELSE '{}' END,
                        '$.transport_status', ?, '$.transport_request_id', ?
                    ) WHERE id=? AND state='started'
                    """,
                    (http_status, request_id or None, operation_id),
                )
    except Exception as exc:
        raise ModelResponseNotSavedError("模型已收到請求但回應無法永久保存；須先對帳，禁止自動重送") from exc
