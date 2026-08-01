from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from PIL import Image, ImageDraw

from inktime.app.domain.photos import ThumbnailCache
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider
from inktime.app.services.batch_analysis import stream_jsonl_shards


def _write_deterministic_photo(path: Path, index: int) -> None:
    width, height = 1400, 900
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for band in range(0, height, 6):
        red = (index * 17 + band // 3) % 256
        green = (index * 29 + band) % 256
        blue = (index * 43 + band * 2) % 256
        draw.rectangle((0, band, width, min(height, band + 6)), fill=(red, green, blue))
    for shape in range(180):
        x = (index * 131 + shape * 73) % width
        y = (index * 197 + shape * 47) % height
        w = 20 + ((index * 17 + shape * 29) % 240)
        h = 20 + ((index * 23 + shape * 31) % 160)
        color = (
            20 + ((index * 41 + shape * 13) % 220),
            20 + ((index * 53 + shape * 17) % 220),
            20 + ((index * 67 + shape * 19) % 220),
        )
        draw.rectangle((x, y, min(width, x + w), min(height, y + h)), outline=color, width=3)
    draw.line((0, height // 2, width, (index * 37) % height), fill=(250, 250, 250), width=5)
    image.save(path, format="JPEG", quality=92, optimize=True)


def test_payload_memory_real_image_100_photo_jsonl(tmp_path):
    source_root = tmp_path / "photos"
    source_root.mkdir()
    source_paths = []
    for index in range(100):
        path = source_root / f"photo-{index:03d}.jpg"
        _write_deterministic_photo(path, index)
        source_paths.append(path)

    cache = ThumbnailCache(tmp_path / "thumbnail-cache")
    provider = OpenAICompatibleProvider(
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key="",
        supports_reasoning_effort=True,
    )
    items = [
        {
            "id": f"item-{index:03d}",
            "custom_id": f"ibt:{index:08x}-0000-0000-0000-{index:012x}",
            "source": path,
            "content_sha256": sha256(path.read_bytes()).hexdigest(),
        }
        for index, path in enumerate(source_paths)
    ]

    def line_factory(item):
        with cache.acquire_for_use(item["source"], item["content_sha256"], 1024) as thumbnail:
            body = provider.build_analysis_request_body(
                image_path=thumbnail,
                model="gpt-5.6-luna",
                detail="high",
                stage="single_high",
                max_tokens=8000,
                reasoning_effort="none",
            )
        return (
            json.dumps(
                {
                    "custom_id": item["custom_id"],
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": body,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    shards = stream_jsonl_shards(
        tmp_path / "batches" / "offline-payload",
        (item for item in items),
        line_factory,
        max_items=500,
        max_bytes=150 * 1024 * 1024,
    )

    assert len(shards) == 1
    shard = shards[0]
    shard_path = Path(shard["path"])
    assert shard["items_count"] == 100
    assert shard_path.stat().st_mode & 0o777 == 0o600
    assert shard["bytes"] == shard_path.stat().st_size
    assert shard["bytes"] > 100 * 1024
    assert shard["peak_rss_bytes"] > 0
    assert shard["peak_rss_bytes"] < 1024 * 1024 * 1024

    custom_ids = set()
    line_count = 0
    with shard_path.open("rb") as stream:
        for raw_line in stream:
            line_count += 1
            assert b"data:image/jpeg;base64," in raw_line
            assert str(source_root).encode("utf-8") not in raw_line
            assert b"photo-" not in raw_line
            payload = json.loads(raw_line)
            custom_ids.add(payload["custom_id"])
            assert payload["method"] == "POST"
            assert payload["url"] == "/v1/chat/completions"
            assert payload["body"]["model"] == "gpt-5.6-luna"
            assert payload["body"]["reasoning_effort"] == "none"
            assert payload["body"]["messages"][0]["content"].startswith("你是 InkTime")
            assert payload["body"]["response_format"]["type"] == "json_schema"
    assert line_count == 100
    assert len(custom_ids) == 100

    byte_shards = stream_jsonl_shards(
        tmp_path / "batches" / "offline-payload-bytes",
        (item for item in items),
        line_factory,
        max_items=500,
        max_bytes=2 * 1024 * 1024,
    )
    assert len(byte_shards) > 1
    assert all(shard["bytes"] <= 2 * 1024 * 1024 for shard in byte_shards)
    assert sum(int(shard["items_count"]) for shard in byte_shards) == 100
