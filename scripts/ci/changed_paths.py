"""Compatibility adapter for the source-owned hosted CI planner.

Domain ownership, suite fan-out, and full/impact semantics live in
scripts.ci.test_plan. This module retains the older boolean output and
validated Git diff CLI so existing workflow consumers can migrate without a
second routing map.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from collections.abc import Iterable

from scripts.ci.test_plan import build_test_plan

CATEGORIES = (
    "docs",
    "python",
    "web_ui",
    "runtime",
    "persistence",
    "docker",
    "tls_security",
    "firmware",
    "provider_ai",
    "benchmark",
    "dependencies",
    "ci_config",
)

_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


def _normalise(path: str) -> str:
    value = str(path).strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def classify_paths(paths: Iterable[str], *, force_full_suite: bool = False) -> dict[str, bool]:
    """Project the canonical structured plan into legacy routing flags."""

    plan = build_test_plan(
        paths,
        {
            "force_full_suite": force_full_suite,
        },
    )
    domains = set(plan["changed_domains"])
    result: dict[str, bool] = {category: category in domains for category in CATEGORIES}
    result["changed"] = bool(plan["changed_paths"])
    result["full_suite"] = plan["ci_mode"] == "full"
    return result


def _validate_commit_sha(value: str, argument_name: str) -> str:
    if _COMMIT_SHA_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{argument_name} must be a complete 40-character hexadecimal commit SHA")
    return value


def _changed_paths(base: str, head: str) -> list[str]:
    git_executable = shutil.which("git")
    if git_executable is None:
        raise RuntimeError("git executable not found in PATH")
    validated_base = _validate_commit_sha(base, "base")
    validated_head = _validate_commit_sha(head, "head")
    completed = subprocess.run(  # noqa: S603
        [
            git_executable,
            "diff",
            "--name-only",
            f"{validated_base}...{validated_head}",
            "--",
        ],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="40-character base commit SHA")
    parser.add_argument("--head", required=True, help="40-character head commit SHA")
    parser.add_argument("--full-suite", action="store_true", help="force every hosted tier")
    args = parser.parse_args()
    flags = classify_paths(_changed_paths(args.base, args.head), force_full_suite=args.full_suite)
    for key in (*CATEGORIES, "changed", "full_suite"):
        value = flags[key]
        print(f"{key}={'true' if value else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
