"""Permanent model inputs/outputs, separate from bounded diagnostic traces.

Headers and credentials are never stored. Exact image payloads are content
addressed under /data/model-inputs; original NAS photos remain read-only.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from inktime.app.db import Database


class ModelEvidenceNotSavedError(RuntimeError):
    not_sent = True


class ModelResponseNotSavedError(RuntimeError):
    code = "VLM-EVIDENCE-RESPONSE"
    ambiguous = True
    request_started = True
    vision_started = True


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


def save_model_transport_response(database: Database, operation_id: str, raw: str) -> None:
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
    except Exception as exc:
        raise ModelResponseNotSavedError("模型已收到請求但回應無法永久保存；須先對帳，禁止自動重送") from exc
