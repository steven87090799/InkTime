"""Manual native OpenAI Batch check, with a durable receipt and no automatic resend."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from inktime.app.domain.analysis.schema import validate_model_response
from inktime.app.domain.photos import ThumbnailCache
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.providers import openai_compatible as provider_contract

TERMINAL = {"completed", "failed", "expired", "cancelled"}
ENDPOINT = "https://api.openai.com/v1"


def _env_images() -> list[Path]:
    values = [Path(value.strip()).expanduser().resolve()
              for value in os.environ.get("INKTIME_LIVE_SMOKE_IMAGES", "").split(",") if value.strip()]
    if not 1 <= len(values) <= 3 or any(not path.is_file() for path in values):
        raise ValueError("INKTIME_LIVE_SMOKE_IMAGES 必須是 1 到 3 張非敏感測試圖片")
    return values


def _save(path: Path, value: dict, *, initial: bool = False) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    if initial:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _receipt_lock(path: Path):
    lock = path.with_name(path.name + ".lock")
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _validate_outputs(remote: dict, expected: list[str], files: list[Path]) -> dict:
    results, seen = [], set()
    usage = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    valid = remote.get("status") == "completed"
    for path in files:
        for raw in path.read_text(encoding="utf-8").splitlines():
            identifier, accepted, code, analysis = None, False, "BATCH-INVALID", None
            try:
                line = json.loads(raw)
                identifier = line["custom_id"]
                if identifier not in expected or identifier in seen:
                    raise ValueError("ID mismatch")
                seen.add(identifier)
                response = line.get("response") or {}
                if line.get("error") or response.get("status_code") != 200:
                    raise ValueError("HTTP error")
                body = response["body"]
                parsed_usage = provider_contract.OpenAICompatibleProvider._usage(body)
                if not parsed_usage.tokens_reported:
                    raise ValueError("Missing or invalid usage evidence")
                for key in usage:
                    usage[key] += getattr(parsed_usage, key)
                choice = body["choices"][0]
                message = choice["message"]
                if choice.get("finish_reason") != "stop" or message.get("refusal"):
                    raise ValueError("Incomplete answer")
                content = message.get("content")
                if isinstance(content, list):
                    content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
                analysis = validate_model_response(content)
                if analysis.get("schema_version") != 5:
                    raise ValueError("Current analysis required")
                accepted, code = True, "OK"
            except (KeyError, IndexError, TypeError, ValueError):
                valid = False
                analysis = None
            results.append({"custom_id": identifier if identifier in expected else None, "accepted": accepted,
                            "code": code, "analysis": analysis})
    counts = remote.get("request_counts") or {}
    valid = bool(valid and seen == set(expected) and len(results) == len(expected)
                 and counts.get("total") == len(expected) and counts.get("completed") == len(expected)
                 and counts.get("failed") == 0)
    return {"accepted": valid, "status": remote.get("status"), "requests": counts,
            "usage": usage, "items": results, "missing_custom_ids": sorted(set(expected) - seen)}


def _rate(name: str) -> float:
    value = float(os.environ.get(name, "0"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必須是經確認的正數；不會猜測付費價格")
    return value


def _prepare(provider, root: Path) -> tuple[Path, dict]:
    images, custom_ids, hashes, request_hashes = _env_images(), [], [], []
    model = os.environ.get("INKTIME_LIVE_SMOKE_MODEL", "").strip()
    if not model:
        raise ValueError("INKTIME_LIVE_SMOKE_MODEL 必須明確指定 native OpenAI 模型")
    budget = _rate("INKTIME_LIVE_SMOKE_MAX_COST_USD")
    input_rate = _rate("INKTIME_LIVE_SMOKE_INPUT_USD_PER_MILLION")
    output_rate = _rate("INKTIME_LIVE_SMOKE_OUTPUT_USD_PER_MILLION")
    input_path, input_bound = root / "input.jsonl", 0
    cache = ThumbnailCache(root / "thumbnail-cache")
    with input_path.open("wb") as stream:
        os.chmod(input_path, 0o600)
        for image in images:
            identifier, digest = f"ibt:{uuid4()}", hashlib.sha256(image.read_bytes()).hexdigest()
            custom_ids.append(identifier)
            hashes.append(digest)
            with cache.acquire_for_use(image, digest, 1024) as thumbnail:
                body = provider.build_analysis_request_body(image_path=thumbnail, model=model, detail="high",
                                                           stage="single", max_tokens=512, reasoning_effort="none")
            # Conservative local input bound, including encoded image bytes; not a provider quote.
            input_bound += len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
            request_hashes.append(hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest())
            stream.write(json.dumps({"custom_id": identifier, "method": "POST", "url": "/v1/chat/completions",
                                     "body": body}, ensure_ascii=False).encode("utf-8") + b"\n")
    estimate = (input_bound * input_rate + len(images) * 512 * output_rate) / 1_000_000
    if estimate > budget:
        raise ValueError("保守成本估計超出明確預算，未上傳／建立 Batch")
    fingerprint = hashlib.sha256(json.dumps({"hashes": hashes, "requests": request_hashes, "model": model, "max_tokens": 512,
                                            "endpoint": ENDPOINT}, sort_keys=True).encode()).hexdigest()
    return input_path, {"version": 1, "endpoint": ENDPOINT, "identity": str(uuid4()), "model": model,
                        "custom_ids": custom_ids, "fingerprint": fingerprint, "estimated_bound_usd": estimate,
                        "budget_usd": budget, "input_rate": input_rate, "output_rate": output_rate,
                        "input_token_bound": input_bound, "state": "prepared"}


def _run(provider, path: Path, *, resume: bool, recovered_id: str | None) -> int:
    with tempfile.TemporaryDirectory(prefix="inktime-live-smoke-") as temporary:
        root = Path(temporary)
        if resume:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if receipt.get("version") != 1 or receipt.get("endpoint") != ENDPOINT:
                raise ValueError("不相容的 receipt")
            if receipt.get("state") == "harvested":
                print(json.dumps(receipt["summary"], ensure_ascii=False))
                return 0 if receipt["summary"]["accepted"] else 1
            remote_id = receipt.get("remote_batch_id")
            if not remote_id and recovered_id:
                remote = provider.retrieve_batch(recovered_id)
                if (remote.get("metadata") or {}).get("inktime_batch_id") != receipt["identity"] or remote.get("input_file_id") != receipt.get("input_file_id"):
                    raise ValueError("遠端 Batch metadata／input 不符，未繫結")
                receipt["remote_batch_id"] = remote_id = recovered_id
                _save(path, receipt)
            if not remote_id:
                print(json.dumps({"state": receipt["state"], "receipt": str(path), "action": "核對 metadata 後以 --remote-batch-id 恢復；不會重送"}, ensure_ascii=False))
                return 3
            remote = provider.retrieve_batch(remote_id)
        else:
            if path.exists():
                raise ValueError("receipt 已存在，請使用 --resume 核對原請求")
            input_path, receipt = _prepare(provider, root)
            _save(path, receipt, initial=True)
            receipt["state"] = "upload_started"
            _save(path, receipt)
            receipt["input_file_id"] = provider.upload_batch_file(input_path, remote_filename=f"inktime-batch-{receipt['identity']}.jsonl")
            receipt["state"] = "create_started"
            _save(path, receipt)  # Durable fence before the ambiguous paid POST.
            remote = provider.create_batch(receipt["input_file_id"], completion_window="24h",
                                           metadata={"inktime_batch_id": receipt["identity"]}, output_expires_after_seconds=86400)
            receipt["remote_batch_id"] = remote["id"]
            print(json.dumps({"remote_batch_id": remote["id"], "receipt": str(path)}, ensure_ascii=False))
            receipt["state"] = "pending"
            _save(path, receipt)
        deadline = time.monotonic() + max(1, int(os.environ.get("INKTIME_LIVE_SMOKE_TIMEOUT_SECONDS", "1800")))
        if (remote.get("metadata") or {}).get("inktime_batch_id") != receipt["identity"] or remote.get("input_file_id") != receipt.get("input_file_id"):
            raise ValueError("遠端 Batch 與 receipt 身分不符，保留結果與檔案供核對")
        while remote.get("status") not in TERMINAL:
            if time.monotonic() >= deadline:
                print(json.dumps({"status": "pending", "remote_batch_id": receipt["remote_batch_id"], "receipt": str(path)}, ensure_ascii=False))
                return 3
            time.sleep(5)
            remote = provider.retrieve_batch(receipt["remote_batch_id"])
        files = []
        for key in ("output_file_id", "error_file_id"):
            if remote.get(key):
                file_path = root / f"{key}.jsonl"
                provider.download_file_content(remote[key], file_path)
                files.append(file_path)
        summary = _validate_outputs(remote, receipt["custom_ids"], files)
        cleanup_ids = sorted({receipt.get("input_file_id"), remote.get("output_file_id"), remote.get("error_file_id")} - {None, ""})
        receipt.update(state="harvested", summary=summary, remote_status=remote.get("status"), remote_file_ids=cleanup_ids)
        _save(path, receipt)  # Persist sanitized analyses and full usage before remote cleanup.
        print(json.dumps(summary, ensure_ascii=False))
        cleanup_failed = []
        for file_id in cleanup_ids:
            try:
                provider.delete_remote_file(file_id)
            except Exception:
                cleanup_failed.append(file_id)
                print(json.dumps({"cleanup": "manual_follow_up_required", "file_id": file_id}))
        receipt["cleanup_failed_file_ids"] = cleanup_failed
        _save(path, receipt)
        return 0 if summary["accepted"] else 1


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("INKTIME_OPENAI_LIVE_SMOKE") != "1":
        print("NOT RUN: set INKTIME_OPENAI_LIVE_SMOKE=1 for an explicit manual live smoke")
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=Path("inktime-openai-batch-smoke-receipt.json"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--remote-batch-id", help="Metadata-verified recovery after an ambiguous create")
    args = parser.parse_args(argv)
    if args.remote_batch_id and not args.resume:
        parser.error("--remote-batch-id requires --resume")
    if os.environ.get("OPENAI_BASE_URL", ENDPOINT).rstrip("/") != ENDPOINT:
        raise ValueError("這是 native OpenAI smoke；不能用來驗收 OpenRouter Batch")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY 未設定")
    provider = OpenAICompatibleProvider(name="OpenAI live smoke", base_url=ENDPOINT, api_key=api_key,
                                       supports_reasoning_effort=True)
    path = args.receipt.expanduser().resolve()
    try:
        with _receipt_lock(path):
            return _run(provider, path, resume=args.resume, recovered_id=args.remote_batch_id)
    except Exception as exc:
        print(json.dumps({"status": "not_accepted", "code": type(exc).__name__, "receipt": str(path),
                          "action": "保留 receipt 並核對原請求；不會自動重送"}, ensure_ascii=False))
        return 1
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
