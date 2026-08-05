"""Classify a GitHub diff into the hosted CI tiers.

The classifier is deliberately dependency-free and fail-open: an unrecognised
path routes the full suite rather than allowing a new production surface to be
silently skipped.  The pure ``classify_paths`` function is covered by hosted
unit tests; the CLI only adds the Git diff and GitHub-output adapters.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from collections.abc import Iterable


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
    """Return stable routing flags for repository-relative changed paths."""

    result = {category: False for category in CATEGORIES}
    normalised = [_normalise(path) for path in paths if _normalise(path)]
    result["changed"] = bool(normalised)
    result["full_suite"] = bool(force_full_suite)

    for path in normalised:
        matched = False
        is_doc = path.startswith("docs/") or path.startswith(("README", "LICENSE")) or path.endswith(".md")

        if path == "AGENTS.md" or path.startswith(".github/"):
            result["ci_config"] = True
            result["full_suite"] = True
            matched = True

        if (
            path.startswith(("requirements", "constraints"))
            or path in {
                "pyproject.toml",
                "setup.cfg",
                "tox.ini",
                "uv.lock",
                "Pipfile.lock",
                ".github/dependabot.yml",
            }
        ):
            result["dependencies"] = True
            result["full_suite"] = True
            matched = True

        if path.startswith("esp32/") or path.startswith("tests/firmware/"):
            result["firmware"] = True
            matched = True

        if (
            path.startswith(("Dockerfile", "docker-compose", ".dockerignore", ".github/tls-smoke/"))
            or path == ".trivyignore"
        ):
            result["docker"] = True
            matched = True

        if (
            path.startswith(("tests/security/", "inktime/app/core/security", "scripts/production_preflight.py"))
            or path.startswith("scripts/production_tls_smoke.py")
            or path.startswith("scripts/secret_")
            or path == ".trivyignore"
        ):
            result["tls_security"] = True
            matched = True

        if (
            path.startswith(("inktime/app/web/", "templates/", "static/", "tests/e2e/"))
            or path.endswith(('.html', '.css', '.js'))
            or path == "tests/integration/test_management_ui.py"
            or any(token in path.casefold() for token in ("/auth", "/session", "/csrf"))
        ):
            result["web_ui"] = True
            matched = True
            if not is_doc and any(token in path.casefold() for token in ("/auth", "/session", "/csrf")):
                result["tls_security"] = True

        if (
            path.startswith(("inktime/app/workers/", "inktime/app/services/scheduler"))
            or path in {
                "inktime/app/db/connection.py",
                "inktime/app/db/migrations.py",
                "scripts/runtime_soak.py",
                "tests/integration/test_scheduler.py",
                "tests/integration/test_resilience_runtime.py",
                "tests/unit/test_runtime_concurrency.py",
            }
            or any(
                token in path
                for token in (
                    "/batch",
                    "/offline_schedule",
                    "/queue",
                    "device_delivery",
                    "device_manifest",
                )
            )
        ):
            result["runtime"] = True
            matched = True

        if (
            path.startswith(("inktime/app/db/", "inktime/app/repositories/analysis_batches.py"))
            or path.startswith(("scripts/restore_backup.py", "scripts/lan_production_gate.py"))
            or path.startswith("tests/unit/test_migrations")
            or path.startswith("tests/unit/test_backups")
            or path.startswith("tests/unit/test_sqlite")
            or path.startswith("tests/integration/test_sqlite_concurrency")
            or "/queue" in path
            or "/batch" in path
        ):
            result["persistence"] = True
            matched = True

        if not is_doc and (
            path.startswith((".github/tls-smoke/", "scripts/production_tls_smoke.py"))
            or any(token in path.casefold() for token in ("/tls", "proxy", "cookie", "csrf", "security"))
        ):
            result["tls_security"] = True
            matched = True

        if not is_doc and (
            path.startswith(("inktime/app/providers/", "inktime/app/domain/analysis/"))
            or path in {
                "inktime/app/services/analysis.py",
                "inktime/app/services/scoring_lab.py",
                "inktime/app/services/provider_contracts.py",
            }
            or path.startswith(("tests/unit/test_provider", "tests/unit/test_openrouter", "tests/unit/test_scoring"))
        ):
            result["provider_ai"] = True
            matched = True

        if not is_doc and (
            path.startswith(("inktime/app/api/devices", "inktime/app/services/device", "inktime/app/repositories/device"))
            or any(
                token in path
                for token in (
                    "manifest",
                    "queue_ack",
                    "render_profile",
                    "device_delivery",
                    "offline_schedule",
                    ".bmp",
                )
            )
        ):
            result["firmware"] = True
            matched = True

        if not is_doc and (
            path in {"scripts/benchmark_models.py", "inktime/app/services/model_benchmark.py"}
            or path.startswith(("tests/unit/test_model_benchmark", "tests/unit/test_benchmark_metrics"))
            or path.startswith("docs/providers/MODEL_BENCHMARK")
        ):
            result["benchmark"] = True
            matched = True

        if is_doc:
            result["docs"] = True
            matched = True

        if path.endswith(".py") or path in {"server.py", "analyze_photos.py", "pyproject.toml"}:
            result["python"] = True
            matched = True

        if path == "scripts/build_release_image.sh":
            result["docker"] = True
            result["full_suite"] = True
            matched = True

        if not matched:
            result["full_suite"] = True

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
    # Arguments are repository-owned validated commit SHAs; shell execution is disabled.
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
    for key, value in {**flags, "full_suite": flags["full_suite"]}.items():
        print(f"{key}={'true' if value else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
