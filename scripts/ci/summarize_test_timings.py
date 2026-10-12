"""Write a bounded slow-test and module summary from hosted pytest JUnit XML."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET


def summarize(path: Path, *, limit: int = 30) -> str:
    rows = []
    modules: dict[str, float] = defaultdict(float)
    for case in ET.parse(path).iter("testcase"):  # noqa: S314 -- locally generated pytest XML
        seconds = float(case.get("time", "0"))
        module = case.get("classname", "unknown")
        name = f"{module}::{case.get('name', 'unknown')}"
        rows.append((seconds, name))
        modules[module] += seconds
    lines = [
        "## Hosted pytest timings", "",
        f"{len(rows)} cases; {sum(modules.values()):.1f}s accumulated test time (setup/call/teardown).",
        "", "### Slowest cases", "", "| Seconds | Test |", "| ---: | --- |",
    ]
    for seconds, name in sorted(rows, reverse=True)[:limit]:
        name = name.replace("|", "\\|").replace("\n", " ").replace("`", "'")
        lines.append(f"| {seconds:.2f} | `{name}` |")
    lines += ["", "### Slowest modules", "", "| Seconds | Module |", "| ---: | --- |"]
    for module, seconds in sorted(modules.items(), key=lambda item: item[1], reverse=True)[:limit]:
        lines.append(f"| {seconds:.2f} | `{module}` |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.junit.is_file():
        print("No JUnit report produced; the original CI failure remains authoritative.")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(summarize(args.junit), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
