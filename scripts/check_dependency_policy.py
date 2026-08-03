#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
EXACT_REQUIREMENT = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+(?:\s*;.+)?$")
ACTION_SHA = re.compile(r"\buses:\s*[^\s]+@([0-9a-fA-F]+)")
PINNED_PYTHON = re.compile(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}(?:\s+AS\s+\w+)?$")


def requirement_errors(path: Path) -> list[str]:
    errors: list[str] = []
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        if not EXACT_REQUIREMENT.fullmatch(line):
            errors.append(f"{path.name}:{number}: dependency 必須使用 exact == pin: {line}")
    return errors


def main() -> int:
    errors: list[str] = []
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-e2e.txt"):
        errors.extend(requirement_errors(ROOT / name))

    docker_lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    python_from = [line.strip() for line in docker_lines if line.strip().startswith("FROM python:")]
    if len(python_from) != 2 or any(PINNED_PYTHON.fullmatch(line) is None for line in python_from):
        errors.append("Dockerfile: builder/runtime Python base 必須使用同一格式的 immutable digest")
    elif python_from[0].split(" AS ", 1)[0] != python_from[1].split(" AS ", 1)[0]:
        errors.append("Dockerfile: builder/runtime Python base digest 不一致")

    for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            match = ACTION_SHA.search(line)
            if match and len(match.group(1)) != 40:
                errors.append(f"{workflow.name}:{number}: GitHub Action 必須 pin 40-char commit SHA")

    if errors:
        print("\n".join(errors))
        return 1
    print("dependency policy: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
