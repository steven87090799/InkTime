"""Run lightweight local checks for changed files; hosted CI owns test execution."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def git(*arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True, encoding="utf-8")  # noqa: S603,S607 -- fixed git argv


def changed_paths(base: str) -> list[str]:
    names = set()
    for arguments in (
        ("diff", "--name-only", "-z", "--diff-filter=ACMR", f"{base}...HEAD"),
        ("diff", "--name-only", "-z", "--diff-filter=ACMR", "HEAD"),
        ("ls-files", "-z", "--others", "--exclude-standard"),
    ):
        names.update(name for name in git(*arguments).split("\0") if name)
    return sorted(names)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()
    git("rev-parse", "--verify", args.base + "^{commit}")
    paths = changed_paths(args.base)
    for arguments in ((f"{args.base}...HEAD",), (), ("--cached",)):
        git("diff", "--check", *arguments)
    count = 0
    for name in paths:
        path = ROOT / name
        if path.is_file() and name.endswith(".py"):
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=name)
            count += 1
    print(f"Diff whitespace and Python syntax: PASS ({count} changed Python files)")
    if any(name.startswith("docs/") or name == "AGENTS.md" for name in paths):
        subprocess.run([sys.executable, "scripts/ci/validate_ai_navigation.py"], cwd=ROOT, check=True)  # noqa: S603 -- fixed validator argv
    if any(name.startswith("requirements") or name in {"Dockerfile", "pyproject.toml", ".github/dependabot.yml"}
           or name.startswith(".github/workflows/") for name in paths):
        subprocess.run([sys.executable, "scripts/check_dependency_policy.py"], cwd=ROOT, check=True)  # noqa: S603 -- fixed validator argv
    workflows = [name for name in paths if name.startswith(".github/") and name.endswith((".yml", ".yaml"))]
    if workflows:
        import yaml
        for name in workflows:
            if (ROOT / name).is_file():
                yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
        print(f"YAML syntax: PASS ({len(workflows)} changed configuration files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
