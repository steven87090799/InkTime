"""This command is intended for DISCOVERY tasks.
Targeted tasks with known files or symbols should use direct rg/bounded reads instead.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
INDEX_PATH = ROOT / "docs" / "AI_CONTEXT_INDEX.json"
PYTHON_SYMBOL = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)")
MARKDOWN_HEADING = re.compile(r"^\s*(#{1,3})\s+(.+?)\s*$")
HTML_HEADING = re.compile(r"<h([1-3])[^>]*>(.*?)</h\1>", re.IGNORECASE)
CPP_SYMBOL = re.compile(
    r"^\s*(?:static\s+|inline\s+|constexpr\s+|extern\s+)?"
    r"(?:[\w:<>*&]+\s+)+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const\s*)?(?:\{|$)"
)


def _relative(path: str) -> Path:
    return ROOT / path


def _symbols(path: Path, limit: int) -> list[tuple[int, str]]:
    if path.suffix == ".py":
        pattern = PYTHON_SYMBOL
    elif path.suffix in {".md", ".markdown"}:
        pattern = MARKDOWN_HEADING
    elif path.suffix in {".html", ".htm"}:
        pattern = HTML_HEADING
    elif path.suffix in {".c", ".cc", ".cpp", ".h", ".hpp", ".ino"}:
        pattern = CPP_SYMBOL
    else:
        return []

    locations: list[tuple[int, str]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line_number, line in enumerate(lines, 1):
        match = pattern.search(line)
        if not match:
            continue
        if path.suffix in {".md", ".markdown"}:
            label = match.group(2)
        elif path.suffix in {".html", ".htm"}:
            label = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        else:
            label = match.group(1)
        locations.append((line_number, label))
        if len(locations) >= limit:
            break
    return locations


def _print_entrypoint(path_value: str, limit: int, *, symbols: bool = False) -> None:
    path = _relative(path_value)
    print(f"  candidate: {path_value}")
    if path.is_dir():
        print("    directory hint only; locate the requested symbol before reading")
        print(f"    next: rg -n --max-count 1 -- '<exact symbol or error>' {shlex.quote(path_value)}")
        return
    for line_number, label in (_symbols(path, limit) if symbols else []):
        print(f"    {line_number}: {label[:160]}")
    print(f"    next: rg -n -- '<exact symbol or term>' {shlex.quote(path_value)}")


def _route(index: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    task_id = index.get("route_aliases", {}).get(task_id, task_id)
    routes = index.get("task_routes", [])
    for route in routes:
        if isinstance(route, dict) and route.get("id") == task_id:
            return route
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", help="task id from docs/AI_CONTEXT_INDEX.json")
    parser.add_argument(
        "--max-items",
        type=int,
        default=6,
        help="maximum hints per section and symbols per entrypoint (default: 6)",
    )
    parser.add_argument("--symbols", action="store_true", help="read candidate source for bounded symbol hints")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.max_items < 1:
        parser.error("--max-items must be positive")

    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    route = _route(index, args.task_id)
    if route is None:
        valid_ids = [route["id"] for route in index["task_routes"]]
        print(f"Unknown task id: {args.task_id}. Choose one of: {', '.join(valid_ids)}")
        return 2

    print(f"task: {route['id']}")
    if route.get("purpose"):
        print(f"purpose: {route['purpose']}")
    print("entrypoints (paths only by default; --symbols reads source):")
    for entrypoint in route["entrypoints"][:args.max_items]:
        _print_entrypoint(entrypoint, args.max_items, symbols=args.symbols)
    print("contracts (on demand):")
    for contract in route["contracts"][:args.max_items]:
        print(f"  - {contract}")
    print("tests (on demand; locate matching functions only):")
    for test_glob in route.get("tests", [])[:args.max_items]:
        print(f"  - {test_glob}")
    if route.get("read_policy"):
        print(f"read_policy: {route['read_policy']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
