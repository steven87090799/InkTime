"""Offline feasibility/cost probe and explicit single-photo address experiment.

Normal analysis, scanner and renderer never submit extra requests. Use a
disposable copy of the database with Migration 71 for enrich/import-landmark.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from inktime.app.db import Database  # noqa: E402
from inktime.app.domain.analysis.location_experiment import (  # noqa: E402
    build_location_experiment, incremental_cost, validate_location_experiment,
)
from inktime.app.providers.geocoding import GeoapifyGeocoder  # noqa: E402
from inktime.app.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from inktime.app.repositories.photo_locations import PhotoLocationRepository  # noqa: E402


# Official Standard / Batch USD per million tokens, checked 2026-10-22.
# Scenarios only: this does not read or change the deployment's model/pricing.
RATES = {"OpenRouter GPT-6 Luna Standard": (.10, .50),
         "OpenRouter DeepSeek V4.1 Flash Baidu promo": (.0342, .1368),
         "OpenRouter Nex-N2.5-Mini": (.025, .10),
         "OpenRouter Gemini 2.5 Flash Lite Standard": (.10, .40)}


def read_json(path):
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("實驗輸入超過 16 MiB")
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, value):
    # Exclusive creation avoids overwriting prior evidence or private inputs.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def photo_row(database_path, photo_id):
    if not database_path.is_file():
        raise ValueError("請指定已遷移到 71 的測試資料庫副本")
    with sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='photo_locations'").fetchone() is None:
            raise ValueError("測試資料庫尚未完成 Migration 71；本工具不會自動遷移")
        row = connection.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if row is None:
        raise ValueError("找不到照片")
    return dict(row)


def enrich(args):
    photo = photo_row(args.database, args.photo_id)
    if not args.write:
        return {"dry_run": True, "photo_id": photo["id"], "network_requests": 0,
                "action": "加上 --write 與 --allow-network 才會在快取未命中時查詢地址"}
    repository = PhotoLocationRepository(Database(args.database))
    place = repository.cached(photo["gps_lat"], photo["gps_lon"], provider="geoapify", language="zh")
    requests_count = 0
    if place is None:
        if not args.allow_network:
            raise ValueError("快取未命中；需要 --allow-network，單次最多 1 個地址請求")
        place = GeoapifyGeocoder(os.environ.get("GEOCODING_API_KEY", "")).reverse(photo["gps_lat"], photo["gps_lon"])
        repository.cache(photo["gps_lat"], photo["gps_lon"], place, provider="geoapify", language="zh")
        requests_count = 1
    place = repository.save_address(photo["id"], photo["sha256"], photo["gps_lat"], photo["gps_lon"], place)
    return {"photo_id": photo["id"], "network_requests": requests_count, "place": place.as_dict()}


def import_landmark(args):
    from inktime.app.domain.photos.places import coordinate_key

    photo = photo_row(args.database, args.photo_id)
    if photo["sha256"] != args.expected_sha256 or coordinate_key(photo["gps_lat"], photo["gps_lon"]) != coordinate_key(args.latitude, args.longitude):
        raise ValueError("結果所屬的照片內容或 GPS 與目前照片不同")
    _, landmark = validate_location_experiment(read_json(args.result))
    if not args.write:
        return {"dry_run": True, "landmark": landmark}
    repository = PhotoLocationRepository(Database(args.database))
    place = repository.save_landmark(photo["id"], args.expected_sha256, args.latitude, args.longitude,
                                     name=landmark["name"], confidence=landmark["confidence"])
    return {"photo_id": photo["id"], "place": place.as_dict()}


def probe(args):
    from PIL import Image
    import tiktoken

    args.output_dir.mkdir(parents=True, exist_ok=True)
    image = args.output_dir / "synthetic-input.jpg"
    if image.exists():
        raise ValueError("請使用新的輸出目錄，保留之前的實驗證據")
    Image.new("RGB", (1024, 768), "#668e91").save(image)
    provider = OpenAICompatibleProvider(name="offline-place-probe", base_url="https://openrouter.ai/api/v1",
                                        api_key="", kind="openrouter", options={"require_parameters": True})
    baseline = provider.build_analysis_request_body(image_path=image, model="openai/gpt-6-luna", detail="high", stage="single",
                                                    max_tokens=1536, reasoning_effort="none")
    variants = {"no_hint": build_location_experiment(baseline),
                "region_only": build_location_experiment(baseline, region_hint="臺北市信義區"),
                "region_and_gps": build_location_experiment(baseline, region_hint="臺北市信義區", latitude=25.0330, longitude=121.5654)}

    def input_text(body):
        # A repeatable text-token proxy, including the entire response schema.
        # Image tokens and server-side schema framing are not measured here.
        return body["messages"][0]["content"] + body["messages"][1]["content"][0]["text"] + json.dumps(body["response_format"], ensure_ascii=False, separators=(",", ":"))

    measurements = {}
    for encoding_name in ("o200k_base", "cl100k_base"):
        encoding = tiktoken.get_encoding(encoding_name)
        baseline_count = len(encoding.encode(input_text(baseline)))
        measurements[encoding_name] = {
            "baseline_input_text_tokens": baseline_count,
            "additional_input_tokens": {key: len(encoding.encode(input_text(value))) - baseline_count for key, value in variants.items()},
            "additional_output_tokens": {name: len(encoding.encode('{"analysis":' + '{}' + ',"landmark":' + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + '}')) - len(encoding.encode('{}'))
                                         for name, value in {"none": {"name": "", "confidence": 0}, "landmark": {"name": "臺北101", "confidence": .95}}.items()},
        }
    selected = measurements["o200k_base"]
    inputs = selected["additional_input_tokens"]["region_only"]
    outputs = selected["additional_output_tokens"]["landmark"]
    estimates = {name: {str(reasoning): round(incremental_cost(photos=100_000, input_tokens=inputs, output_tokens=outputs,
                                                            reasoning_tokens=reasoning, input_rate=rates[0], output_rate=rates[1]), 4)
                        for reasoning in (0, 128, 512, 2000)}
                 for name, rates in RATES.items()}
    result = {"date": "2026-10-22", "photos": 100_000, "measurements": measurements,
              "assumption": "new analysis already needed; same image and call; no extra reasoning measured; no cache discount",
              "image_requests_added": 0, "image_decodes_added": 0, "actual_api_calls": 0,
              "usd_rates_per_million": RATES, "usd_by_added_reasoning_tokens": estimates}
    save_json(args.output_dir / "token-cost.json", result)
    # Output request JSON is a dry-run sample, never sent by this tool.
    save_json(args.output_dir / "request-region-only.json", variants["region_only"])
    save_json(args.output_dir / "request-region-and-gps.json", variants["region_and_gps"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("probe", help="只使用合成圖片；不呼叫模型或地址 API；需要開發用 tiktoken")
    command.add_argument("--output-dir", type=Path, required=True)
    command = commands.add_parser("build-request", help="從現有 v6 請求建立可檢查的同次分析原型，不送出")
    command.add_argument("--body", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--region-hint", default="")
    command.add_argument("--latitude", type=float)
    command.add_argument("--longitude", type=float)
    for name in ("enrich", "import-landmark"):
        command = commands.add_parser(name)
        command.add_argument("--database", type=Path, required=True, help="已遷移的測試副本")
        command.add_argument("--photo-id", required=True)
        command.add_argument("--write", action="store_true")
        if name == "enrich":
            command.add_argument("--allow-network", action="store_true")
        else:
            command.add_argument("--expected-sha256", required=True)
            command.add_argument("--latitude", type=float)
            command.add_argument("--longitude", type=float)
            command.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build-request":
        save_json(args.output, build_location_experiment(read_json(args.body), region_hint=args.region_hint,
                                                       latitude=args.latitude, longitude=args.longitude))
        result = {"request_saved": str(args.output), "actual_api_calls": 0}
    else:
        result = {"probe": probe, "enrich": enrich, "import-landmark": import_landmark}[args.command](args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
