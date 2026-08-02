#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from inktime.app.core.preflight import (  # noqa: E402
    PreflightError,
    run_production_preflight,
    validate_lan_environment,
)
from inktime.app.core.runtime_config import RuntimeConfig  # noqa: E402


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="InkTime production deployment preflight")
    parser.add_argument("--mode", choices=("lan", "https"), required=True)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    environ = dict(os.environ)
    if args.env_file is not None:
        environ.update(_env_file(args.env_file))
    try:
        config = RuntimeConfig.from_sources(environ=environ, base_dir=ROOT)
        if args.mode == "lan":
            validate_lan_environment(environ, (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        result = run_production_preflight(
            config,
            mode=args.mode,
            allow_test_host=environ.get("INKTIME_LAN_TEST_MODE", "0") == "1",
        )
    except (PreflightError, ValueError) as exc:
        payload = {
            "status": "error",
            "error_code": getattr(exc, "code", "PREFLIGHT-CONFIG-001"),
            "message": getattr(exc, "message", str(exc)),
            "fix": getattr(exc, "fix", "修正 env 後重新執行 preflight"),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result.summary(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
