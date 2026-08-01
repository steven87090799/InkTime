"""Manual, opt-in OpenAI Batch contract smoke test.

This script is deliberately not part of CI.  It requires an explicit gate and
one to three non-sensitive local JPEGs.  It is a small contract check before a
100-photo Sample; it does not use the application database or claim a
production verification result.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from inktime.app.domain.photos import ThumbnailCache
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider


def _env_images() -> list[Path]:
    values = [
        Path(value.strip()).expanduser().resolve()
        for value in os.environ.get("INKTIME_LIVE_SMOKE_IMAGES", "").split(",")
        if value.strip()
    ]
    if not 1 <= len(values) <= 3:
        raise ValueError("INKTIME_LIVE_SMOKE_IMAGES 必須是 1 到 3 張非敏感測試圖片")
    if any(not path.is_file() for path in values):
        raise ValueError("指定的 live smoke 圖片不存在")
    return values


def main() -> int:
    if os.environ.get("INKTIME_OPENAI_LIVE_SMOKE") != "1":
        print("NOT RUN: set INKTIME_OPENAI_LIVE_SMOKE=1 for an explicit manual live smoke")
        return 2
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY 未設定；不會從檔案或 log 讀取 API Key")
    images = _env_images()
    provider = OpenAICompatibleProvider(
        name="OpenAI live smoke",
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=api_key,
        supports_reasoning_effort=True,
    )
    batch_identity = str(uuid4())
    input_file_id: str | None = None
    remote_batch_id: str | None = None
    output_file_id: str | None = None
    error_file_id: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="inktime-live-smoke-") as temporary:
            root = Path(temporary)
            cache = ThumbnailCache(root / "thumbnail-cache")
            input_path = root / "input.jsonl"
            with input_path.open("wb") as stream:
                os.chmod(input_path, 0o600)
                for image in images:
                    custom_id = f"ibt:{uuid4()}"
                    digest = hashlib.sha256(image.read_bytes()).hexdigest()
                    with cache.acquire_for_use(image, digest, 1024) as thumbnail:
                        body = provider.build_analysis_request_body(
                            image_path=thumbnail,
                            model=os.environ.get("INKTIME_LIVE_SMOKE_MODEL", "gpt-5.6-luna"),
                            detail="high",
                            stage="single_high",
                            max_tokens=8000,
                            reasoning_effort="none",
                        )
                    line = {
                        "custom_id": custom_id,
                        "method": "POST",
                        "url": "/v1/chat/completions",
                        "body": body,
                    }
                    stream.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
            input_file_id = provider.upload_batch_file(
                input_path, remote_filename=f"inktime-batch-{batch_identity}.jsonl"
            )
            remote = provider.create_batch(
                input_file_id,
                completion_window="24h",
                metadata={"inktime_batch_id": batch_identity, "inktime_version": "batch-lifecycle-v1"},
                output_expires_after_seconds=86400,
            )
            remote_batch_id = str(remote["id"])
            deadline = time.monotonic() + max(
                60, int(os.environ.get("INKTIME_LIVE_SMOKE_TIMEOUT_SECONDS", "1800"))
            )
            while str(remote.get("status")) not in {"completed", "failed", "expired", "cancelled"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError("live smoke 遠端 Batch 超過 bounded timeout")
                time.sleep(5)
                remote = provider.retrieve_batch(remote_batch_id)
            output_file_id = str(remote.get("output_file_id") or "") or None
            error_file_id = str(remote.get("error_file_id") or "") or None
            if output_file_id:
                provider.download_file_content(output_file_id, root / "output.jsonl")
            if error_file_id:
                provider.download_file_content(error_file_id, root / "error.jsonl")
            usage = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
            if output_file_id:
                for raw_line in (root / "output.jsonl").read_text(encoding="utf-8").splitlines():
                    try:
                        body = json.loads(raw_line).get("response", {}).get("body", {})
                        details = body.get("prompt_tokens_details") or body.get("input_tokens_details") or {}
                        completion = body.get("completion_tokens_details") or {}
                        body_usage = body.get("usage") or {}
                        usage["input_tokens"] += int(
                            body_usage.get("prompt_tokens", body_usage.get("input_tokens", 0)) or 0
                        )
                        usage["cached_tokens"] += int(details.get("cached_tokens", 0) or 0)
                        usage["output_tokens"] += int(
                            body_usage.get("completion_tokens", body_usage.get("output_tokens", 0)) or 0
                        )
                        usage["reasoning_tokens"] += int(completion.get("reasoning_tokens", 0) or 0)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
            print(
                json.dumps(
                    {
                        "status": remote.get("status"),
                        "requests": remote.get("request_counts"),
                        "usage": usage,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        cleanup_ids = (
            (input_file_id, output_file_id, error_file_id) if remote_batch_id else (None, None, None)
        )
        for file_id in cleanup_ids:
            if file_id:
                try:
                    provider.delete_remote_file(file_id)
                except Exception as exc:
                    print(
                        json.dumps(
                            {
                                "cleanup": "manual_follow_up_required",
                                "code": str(getattr(exc, "code", "cleanup_failed")),
                            },
                            ensure_ascii=False,
                        )
                    )
        provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
