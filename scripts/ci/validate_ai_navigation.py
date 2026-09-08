"""Validate the repository's machine-readable AI reading boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
INDEX_PATH = ROOT / "docs" / "AI_CONTEXT_INDEX.json"
NAVIGATION_PATH = ROOT / "docs" / "AI_NAVIGATION.md"
AGENTS_PATH = ROOT / "AGENTS.md"
CONTEXT_TOOL_PATH = ROOT / "scripts" / "ci" / "ai_context.py"


def _relative_path(value: Any, label: str, errors: list[str]) -> Path | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        errors.append(f"{label} must be a non-empty repository-relative path")
        return None
    candidate = (ROOT / value).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError:
        errors.append(f"{label} escapes the repository: {value}")
        return None
    return candidate


def _check_existing_paths(values: Any, label: str, errors: list[str]) -> None:
    if not isinstance(values, list) or not values:
        errors.append(f"{label} must be a non-empty list")
        return
    for index, value in enumerate(values):
        candidate = _relative_path(value, f"{label}[{index}]", errors)
        if candidate is not None and not candidate.exists():
            errors.append(f"{label}[{index}] does not exist: {value}")


def _check_test_globs(values: Any, label: str, errors: list[str]) -> None:
    if not isinstance(values, list):
        errors.append(f"{label} must be a list")
        return
    for index, value in enumerate(values):
        if (
            not isinstance(value, str)
            or not value
            or Path(value).is_absolute()
            or ".." in Path(value).parts
        ):
            errors.append(f"{label}[{index}] must be a repository-relative glob")
            continue
        matches = [path for path in ROOT.glob(value) if path.is_file()]
        if not matches:
            errors.append(f"{label}[{index}] matches no files: {value}")


def validate() -> list[str]:
    errors: list[str] = []
    try:
        index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read {INDEX_PATH.relative_to(ROOT)}: {exc}"]

    if not isinstance(index, dict):
        return ["AI context index must contain a JSON object"]
    if index.get("schema_version") != 1:
        errors.append("AI context index schema_version must be 1")
    if index.get("context_command") != "python scripts/ci/ai_context.py <task-id>":
        errors.append("context_command must point to the bounded AI context tool")
    if not CONTEXT_TOOL_PATH.is_file():
        errors.append("bounded AI context tool is missing: scripts/ci/ai_context.py")

    policy = index.get("default_policy")
    if not isinstance(policy, dict):
        errors.append("default_policy must be an object")
    else:
        if policy.get("first_pass_max_files") != 4:
            errors.append("default_policy.first_pass_max_files must be 4")
        if policy.get("first_pass_max_lines") != 800:
            errors.append("default_policy.first_pass_max_lines must be 800")
        if policy.get("expand_requires_evidence") is not True:
            errors.append("default_policy.expand_requires_evidence must be true")
        if policy.get("full_audit_requires_explicit_request") is not True:
            errors.append("default_policy.full_audit_requires_explicit_request must be true")
        forbidden = policy.get("forbidden_globs")
        if not isinstance(forbidden, list):
            errors.append("default_policy.forbidden_globs must be a list")
        else:
            required = {".env*", "*.db", "data/**", "docs/archive/**"}
            missing = sorted(required - set(forbidden))
            if missing:
                errors.append(f"default_policy.forbidden_globs missing: {', '.join(missing)}")

    routes = index.get("task_routes")
    route_ids: set[str] = set()
    if not isinstance(routes, list) or not routes:
        errors.append("task_routes must be a non-empty list")
    else:
        for route_index, route in enumerate(routes):
            label = f"task_routes[{route_index}]"
            if not isinstance(route, dict):
                errors.append(f"{label} must be an object")
                continue
            route_id = route.get("id")
            if not isinstance(route_id, str) or not route_id:
                errors.append(f"{label}.id must be non-empty")
            elif route_id in route_ids:
                errors.append(f"duplicate task route id: {route_id}")
            else:
                route_ids.add(route_id)
            _check_existing_paths(route.get("entrypoints"), f"{label}.entrypoints", errors)
            _check_existing_paths(route.get("contracts"), f"{label}.contracts", errors)
            _check_test_globs(route.get("tests", []), f"{label}.tests", errors)

    large_files = index.get("large_files")
    if not isinstance(large_files, list) or not large_files:
        errors.append("large_files must be a non-empty list")
    else:
        for file_index, entry in enumerate(large_files):
            label = f"large_files[{file_index}]"
            if not isinstance(entry, dict):
                errors.append(f"{label} must be an object")
                continue
            candidate = _relative_path(entry.get("path"), f"{label}.path", errors)
            minimum = entry.get("min_bytes")
            if not isinstance(minimum, int) or minimum <= 0:
                errors.append(f"{label}.min_bytes must be a positive integer")
            elif candidate is not None:
                if not candidate.is_file():
                    errors.append(f"{label}.path is not a file: {entry.get('path')}")
                elif candidate.stat().st_size < minimum:
                    errors.append(f"{label}.path is smaller than its threshold: {entry.get('path')}")
            if not isinstance(entry.get("policy"), str) or not entry["policy"].strip():
                errors.append(f"{label}.policy must be non-empty")

    required_links = index.get("required_navigation_links")
    _check_existing_paths(required_links, "required_navigation_links", errors)
    required_links_by_file = {
        NAVIGATION_PATH: ("AI_CONTEXT_INDEX.json", "../AGENTS.md"),
        AGENTS_PATH: ("docs/AI_CONTEXT_INDEX.json", "docs/AI_NAVIGATION.md"),
        ROOT / "README.md": ("docs/AI_NAVIGATION.md", "docs/AI_CONTEXT_INDEX.json"),
        ROOT / "README.en.md": ("docs/AI_NAVIGATION.md", "docs/AI_CONTEXT_INDEX.json"),
        ROOT / "USER_MANUAL.html": ("docs/AI_NAVIGATION.md", "docs/AI_CONTEXT_INDEX.json"),
        ROOT / "CLAUDE.md": ("docs/AI_NAVIGATION.md", "docs/AI_CONTEXT_INDEX.json"),
        ROOT / "docs" / "README.md": ("AI_NAVIGATION.md", "AI_CONTEXT_INDEX.json"),
    }
    for path, required_links in required_links_by_file.items():
        content = path.read_text(encoding="utf-8")
        for required in required_links:
            if required not in content:
                errors.append(f"{path.relative_to(ROOT)} does not reference {required}")
    navigation_content = NAVIGATION_PATH.read_text(encoding="utf-8")
    for route_id in sorted(route_ids):
        if f"`{route_id}`" not in navigation_content:
            errors.append(f"docs/AI_NAVIGATION.md does not list task route id: {route_id}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"AI navigation contract: {error}")
        return 1
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    print(
        "AI navigation contract OK: "
        f"{len(index['task_routes'])} routes, "
        f"{len(index['large_files'])} large-file policies"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
