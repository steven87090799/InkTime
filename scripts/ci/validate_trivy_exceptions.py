"""Require machine-readable expiration on every Trivy suppression."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import re


def validate_exceptions(text: str, *, today: date) -> dict[str, int]:
    active = expired = 0
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)\s+exp:(\d{4}-\d{2}-\d{2})", entry)
        if match is None or match[1] in seen:
            raise ValueError(f"Trivy exception line {number} requires a unique ID and exp:YYYY-MM-DD")
        seen.add(match[1])
        deadline = date.fromisoformat(match[2])
        if deadline <= today:
            expired += 1
        else:
            active += 1
    return {"active": active, "expired": expired}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=Path(".trivyignore"))
    args = parser.parse_args()
    counts = validate_exceptions(args.path.read_text(encoding="utf-8"), today=datetime.now(timezone.utc).date())
    print(f"Trivy expiry contract OK: {counts['active']} active; {counts['expired']} expired (not suppressed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
