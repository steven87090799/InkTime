#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility via requirements-dev.txt.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
EXACT_REQUIREMENT = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+(?:\s*;.+)?$")
ACTION_SHA = re.compile(r"\buses:\s*[^\s]+@([0-9a-fA-F]+)")
PINNED_PYTHON = re.compile(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}(?:\s+AS\s+\w+)?$")
PYTHON_310 = Version("3.10")


def requirement_errors(path: Path) -> list[str]:
    errors: list[str] = []
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        if not EXACT_REQUIREMENT.fullmatch(line):
            errors.append(f"{path.name}:{number}: dependency 必須使用 exact == pin: {line}")
    return errors


def requires_python_accepts_310(value: object) -> bool:
    """Return whether a PEP 440 ``requires-python`` specifier accepts Python 3.10."""

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        specifier = SpecifierSet(value)
    except InvalidSpecifier:
        return False
    return specifier.contains(PYTHON_310, prereleases=True)


def pyproject_errors(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return [f"{path.name}: invalid TOML: {exc}"]

    project = document.get("project")
    if not isinstance(project, dict):
        return ["pyproject.toml: [project] metadata is required"]

    requires_python = project.get("requires-python")
    if not requires_python_accepts_310(requires_python):
        errors.append(
            "pyproject.toml: [project].requires-python must semantically allow Python 3.10"
        )

    dynamic = project.get("dynamic")
    if not isinstance(dynamic, list) or "version" not in dynamic:
        errors.append("pyproject.toml: [project].dynamic must declare version")

    build_system = document.get("build-system")
    if not isinstance(build_system, dict):
        errors.append("pyproject.toml: [build-system] metadata is required")
    else:
        if build_system.get("build-backend") != "setuptools.build_meta":
            errors.append("pyproject.toml: build backend must be setuptools.build_meta")
        requirements = build_system.get("requires")
        if not isinstance(requirements, list) or not any(
            isinstance(requirement, str) and requirement.casefold().startswith("setuptools")
            for requirement in requirements
        ):
            errors.append("pyproject.toml: build-system.requires must retain setuptools")

    tool = document.get("tool")
    setuptools = tool.get("setuptools") if isinstance(tool, dict) else None
    setuptools_dynamic = setuptools.get("dynamic") if isinstance(setuptools, dict) else None
    dynamic_version = (
        setuptools_dynamic.get("version") if isinstance(setuptools_dynamic, dict) else None
    )
    if not isinstance(dynamic_version, dict) or dynamic_version.get("attr") != "inktime._version.__version__":
        errors.append(
            "pyproject.toml: setuptools dynamic version must use inktime._version.__version__"
        )
    return errors


def main() -> int:
    errors: list[str] = []
    errors.extend(pyproject_errors(ROOT / "pyproject.toml"))
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
