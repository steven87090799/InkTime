#!/usr/bin/env python3
"""Run the bounded model benchmark; offline request-contract mode is default."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from inktime.app.services.model_benchmark import (
    BenchmarkError,
    ModelBenchmarkService,
    write_report,
)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _sides(value: str) -> list[int]:
    return [int(item) for item in _csv(value)]


def main() -> int:
    parser = argparse.ArgumentParser(description="InkTime bounded model benchmark")
    parser.add_argument("--live", action="store_true", help="explicitly permit external Provider calls")
    parser.add_argument("--provider", default="offline-synthetic")
    parser.add_argument("--models", default="offline/synthetic")
    parser.add_argument("--sample-count", type=int, default=3)
    parser.add_argument("--seed", default="inktime-benchmark-v1")
    parser.add_argument("--image-sides", default="512,1024,1600")
    parser.add_argument("--prompt-profiles", default="default,advanced")
    parser.add_argument("--variants", default="off,on", choices=["off", "on", "off,on", "on,off"])
    parser.add_argument("--reasoning", default="none,low")
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--max-cost", type=float, default=1.0)
    parser.add_argument("--dataset", type=Path, help="live quality golden manifest; never a production photo path")
    parser.add_argument(
        "--confirm-live-quality",
        action="store_true",
        help="acknowledge that live quality mode can incur Provider cost",
    )
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--api-key", default=os.environ.get("INKTIME_BENCHMARK_API_KEY", ""))
    parser.add_argument("--output", type=Path, default=Path("data/benchmarks/latest.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("data/benchmarks/latest.md"))
    args = parser.parse_args()
    try:
        variants = [item == "on" for item in _csv(args.variants)]
        service = ModelBenchmarkService()
        axes = service.build_axes(
            provider=args.provider,
            models=_csv(args.models),
            image_sides=_sides(args.image_sides),
            prompt_profiles=_csv(args.prompt_profiles),
            variants=variants,
            reasoning_efforts=_csv(args.reasoning),
            options=(
                {"data_collection": "deny", "zdr": True}
                if str(args.provider).casefold() == "openrouter"
                else {}
            ),
        )
        report = (
            service.run_live(
                axes=axes,
                sample_count=args.sample_count,
                seed=args.seed,
                api_key=args.api_key,
                base_url=args.base_url,
                max_requests=args.max_requests,
                max_cost=args.max_cost,
                dataset=args.dataset,
                confirm_live_quality=args.confirm_live_quality,
            )
            if args.live
            else service.run_offline(axes=axes, sample_count=args.sample_count, seed=args.seed)
        )
        write_report(report, output=args.output, markdown_output=args.markdown_output)
        print(json.dumps({"mode": report["mode"], "axes": len(report["axes"]), "output": str(args.output)}, ensure_ascii=False))
        return 0
    except (BenchmarkError, ValueError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
