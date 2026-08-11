from __future__ import annotations

import json

from inktime.app.core.ai_trace import (
    IMAGE_PAYLOAD_REDACTED,
    SECRET_REDACTED,
    sanitize_ai_payload,
    sanitized_json_text,
)


def test_ai_trace_sanitizer_removes_secrets_and_image_payloads() -> None:
    body = {
        "model": "vision-model",
        "max_tokens": 2048,
        "headers": {"Authorization": "Bearer secret-value", "Cookie": "session=secret"},
        "api_key": "sk-secret-value",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "分析這張照片"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + "A" * 4096, "detail": "high"},
                    },
                ],
            }
        ],
    }

    clean = sanitize_ai_payload(
        body,
        photo={
            "id": "photo-1",
            "sha256": "abc123",
            "width": 4032,
            "height": 3024,
            "mime_type": "image/jpeg",
        },
    )
    image = clean["messages"][0]["content"][1]
    assert clean["headers"] == {"Authorization": SECRET_REDACTED, "Cookie": SECRET_REDACTED}
    assert clean["api_key"] == SECRET_REDACTED
    assert clean["max_tokens"] == 2048
    assert image == {
        "type": "input_image",
        "photo_id": "photo-1",
        "sha256": "abc123",
        "width": 4032,
        "height": 3024,
        "mime_type": "image/jpeg",
        "detail": "high",
        "payload": IMAGE_PAYLOAD_REDACTED,
    }
    stored = sanitized_json_text(clean)
    assert "Bearer secret-value" not in stored
    assert "sk-secret-value" not in stored
    assert "data:image" not in stored
    assert "A" * 256 not in stored
    assert json.loads(stored)["messages"][0]["content"][1]["payload"] == IMAGE_PAYLOAD_REDACTED
