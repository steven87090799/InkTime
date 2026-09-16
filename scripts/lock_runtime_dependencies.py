#!/usr/bin/env python3
"""Resolve target Linux wheels without installing or building application packages."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def lock_lines(report: dict, target: str) -> list[str]:
    lines = [
        f"# Python 3.12 / Linux {target}; generated from requirements.txt.",
        "# Wheel hashes include all resolved transitive runtime dependencies.",
        "# Refresh with scripts/lock_runtime_dependencies.py.",
    ]
    for item in sorted(report["install"], key=lambda item: item["metadata"]["name"].lower()):
        metadata = item["metadata"]
        digest = item["download_info"]["archive_info"]["hashes"]["sha256"]
        lines.append(f"{metadata['name']}=={metadata['version']} --hash=sha256:{digest}")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "requirements-locks")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("Run with Python 3.12 (matching the container)")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="inktime-runtime-lock-") as temporary:
        for architecture, target in (("x86_64", "amd64"), ("aarch64", "arm64")):
            report = Path(temporary) / f"{target}.json"
            command = [
                sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
                "--only-binary=:all:", "--python-version", "3.12", "--implementation", "cp",
                "--abi", "cp312", "--abi", "abi3", "--abi", "none",
                "--index-url", "https://pypi.org/simple",
            ]
            for platform in ("manylinux_2_34", "manylinux_2_28", "manylinux_2_17", "manylinux2014"):
                command += ["--platform", f"{platform}_{architecture}"]
            command += ["--report", str(report), "-r", str(ROOT / "requirements.txt")]
            subprocess.run(command, check=True)  # noqa: S603 -- argv only, fixed pip operation
            lines = lock_lines(json.loads(report.read_text()), target)
            (args.output / f"linux-{target}-py312.txt").write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
