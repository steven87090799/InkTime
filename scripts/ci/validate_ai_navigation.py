"""Validate the repository's machine-readable AI reading boundary."""

from __future__ import annotations

import ast
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
        deferred = policy.get("deferred_globs")
        if not isinstance(deferred, list):
            errors.append("default_policy.deferred_globs must be a list")
        else:
            required = {".env*", "*.db", "data/**", "docs/archive/**"}
            missing = sorted(required - set(deferred))
            if missing:
                errors.append(f"default_policy.deferred_globs missing: {', '.join(missing)}")

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

    large_policy = index.get("large_file_policy", {})
    if large_policy.get("threshold_bytes") != 50000 or "symbol-first" not in large_policy.get("policy", ""):
        errors.append("large_file_policy must apply symbol-first above 50000 bytes")
    if isinstance(policy, dict) and policy.get("targeted_skip_context_tool") is not True:
        errors.append("TARGETED must skip ai_context")
    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue
            entries = route.get("entrypoints", [])
            if len(entries) > 4:
                errors.append(f"route {route.get('id')} has more than 4 entrypoints")
            for value in entries:
                if isinstance(value, str) and ((ROOT / value).is_dir() or value.startswith("docs/archive/")):
                    errors.append(f"route entrypoints must be current file-level candidates: {value}")
            if any(str(value).startswith("docs/archive/") for value in route.get("contracts", [])):
                errors.append("historical contracts must not be initial route context")
    aliases = index.get("route_aliases", {})
    if not isinstance(aliases, dict):
        errors.append("route_aliases must be an object")
    else:
        for alias, target in aliases.items():
            if alias in route_ids or target not in route_ids:
                errors.append(f"invalid route alias: {alias} -> {target}")

    # Static regression checks run in the existing Hosted navigation gate; no pytest needed.
    tool_ast = ast.parse(CONTEXT_TOOL_PATH.read_text(encoding="utf-8"))
    defaults = [
        kw.value.value
        for node in ast.walk(tool_ast)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and any(isinstance(arg, ast.Constant) and arg.value == "--max-items" for arg in node.args)
        for kw in node.keywords if kw.arg == "default" and isinstance(kw.value, ast.Constant)
    ]
    if defaults != [12]:
        errors.append("ai_context --max-items default must be 12")
    agents = AGENTS_PATH.read_text(encoding="utf-8")
    for marker in ("TARGETED", "Skip `docs/AI_NAVIGATION.md`", "50 KB", "symbol-first",
                   "Tests are symbol-first", "PR review is diff-first", "git diff <base>...HEAD",
                   "docs/archive/**", "BASE_HEAD=", "FINAL_HEAD=", "CI_STATUS="):
        if marker not in agents:
            errors.append(f"AGENTS context rule missing: {marker}")
    navigation = NAVIGATION_PATH.read_text(encoding="utf-8")
    for marker in ("僅 DISCOVERY / FULL_AUDIT", "不要跑 ai_context.py", "TARGETED → exact rg"):
        if marker not in navigation:
            errors.append(f"optional navigation rule missing: {marker}")
    for path in (AGENTS_PATH, NAVIGATION_PATH):
        content = path.read_text(encoding="utf-8")
        for obsolete in ("Before editing, read [`docs/AI_NAVIGATION.md`]", "每次任務的閱讀順序",
                         "Read both hardware contracts in full", "2. Read [docs/AI_NAVIGATION.md]"):
            if obsolete in content:
                errors.append(f"mandatory discovery/historical reading returned: {path.name}")

    required_links = index.get("required_navigation_links")
    _check_existing_paths(required_links, "required_navigation_links", errors)
    required_links_by_file = {
        NAVIGATION_PATH: ("AI_CONTEXT_INDEX.json", "../AGENTS.md"),
        AGENTS_PATH: ("docs/AI_CONTEXT_INDEX.json", "docs/AI_NAVIGATION.md"),
        ROOT / "README.md": ("docs/AI_NAVIGATION.md", "docs/AI_CONTEXT_INDEX.json"),
        ROOT / "README.en.md": ("docs/AI_NAVIGATION.md", "docs/AI_CONTEXT_INDEX.json"),
        ROOT / "USER_MANUAL.html": ("docs/AI_NAVIGATION.md", "docs/AI_CONTEXT_INDEX.json"),
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
        "general symbol-first policy (>50000 bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
