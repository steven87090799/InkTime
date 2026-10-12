"""Repair same-repository Dependabot runtime locks using trusted base-branch code."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATHS = (
    "requirements-locks/linux-amd64-py312.txt",
    "requirements-locks/linux-arm64-py312.txt",
)
PIN = re.compile(r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+$")


def eligible_pull(pull: dict, repository: str, branch: str, sha: str) -> bool:
    return (
        pull.get("state") == "open"
        and pull.get("user", {}).get("login") == "dependabot[bot]"
        and branch.startswith("dependabot/pip/")
        and pull.get("head", {}).get("ref") == branch
        and pull.get("head", {}).get("sha") == sha
        and (pull.get("head", {}).get("repo") or {}).get("full_name") == repository
        and pull.get("base", {}).get("ref") == "main"
        and (pull.get("base", {}).get("repo") or {}).get("full_name") == repository
    )


def validate_requirements(text: str) -> None:
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not PIN.fullmatch(line):
            raise ValueError("Automatic lock repair accepts exact package pins only")
        package = re.sub(r"[-_.]+", "-", line.split("==", 1)[0]).lower()
        if package in seen:
            raise ValueError("Duplicate runtime package pin")
        seen.add(package)
    if not seen:
        raise ValueError("Empty runtime requirements")


def api(repository: str, path: str, *, method: str = "GET", body: dict | None = None):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 -- fixed GitHub API origin
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("GitHub response exceeds size limit")
    return json.loads(data)


def file_text(repository: str, path: str, sha: str) -> str:
    document = api(repository, f"contents/{path}?ref={sha}")
    if document.get("type") != "file" or document.get("encoding") != "base64":
        raise ValueError("Expected a small repository text file")
    return base64.b64decode(document["content"]).decode("utf-8")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from scripts.check_dependency_policy import runtime_lock_errors

    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    run = event["workflow_run"]
    repository = os.environ["GITHUB_REPOSITORY"]
    branch, sha = run["head_branch"], run["head_sha"]
    if not branch.startswith("dependabot/pip/"):
        return 0
    query = urllib.parse.urlencode({"head": repository.split("/")[0] + ":" + branch, "state": "open"})
    pulls = api(repository, "pulls?" + query)
    matches = [pull for pull in pulls if eligible_pull(pull, repository, branch, sha)]
    if len(matches) != 1:
        print("No current same-repository Dependabot runtime PR; no files changed.")
        return 0
    pull = matches[0]
    requirements = file_text(repository, "requirements.txt", sha)
    if requirements == file_text(repository, "requirements.txt", pull["base"]["sha"]):
        return 0
    validate_requirements(requirements)
    original = {path: file_text(repository, path, sha) for path in LOCK_PATHS}
    with tempfile.TemporaryDirectory(prefix="inktime-dependabot-lock-") as directory:
        temporary = Path(directory)
        requirements_path = temporary / "requirements.txt"
        requirements_path.write_text(requirements, encoding="utf-8")
        locks = temporary / "requirements-locks"
        locks.mkdir()
        for path, text in original.items():
            (locks / Path(path).name).write_text(text, encoding="utf-8")
        if not any(runtime_lock_errors(locks / Path(path).name, requirements_path) for path in LOCK_PATHS):
            print("Runtime locks already satisfy dependency policy.")
            return 0
        subprocess.run(  # noqa: S603 -- trusted generator, argv only; PR pins are validated data
            [sys.executable, str(ROOT / "scripts/lock_runtime_dependencies.py"),
             "--requirements", str(requirements_path), "--output", str(locks)],
            check=True,
        )
        for path in LOCK_PATHS:
            if runtime_lock_errors(locks / Path(path).name, requirements_path):
                raise ValueError("Generated runtime lock violates dependency policy")
        replacements = {
            path: (locks / Path(path).name).read_text(encoding="utf-8")
            for path in LOCK_PATHS
            if (locks / Path(path).name).read_text(encoding="utf-8") != original[path]
        }
    current = api(repository, f"pulls/{pull['number']}")
    if not eligible_pull(current, repository, branch, sha):
        print("PR changed during resolution; no branch update attempted.")
        return 0
    commit = api(repository, f"git/commits/{sha}")
    tree = api(repository, "git/trees", method="POST", body={
        "base_tree": commit["tree"]["sha"],
        "tree": [
            {"path": path, "mode": "100644", "type": "blob", "content": text}
            for path, text in replacements.items()
        ],
    })
    repaired = api(repository, "git/commits", method="POST", body={
        "message": "fix(deps): regenerate target runtime wheel locks",
        "tree": tree["sha"], "parents": [sha],
    })
    # A sibling update races with this parent and is rejected as non-fast-forward.
    api(repository, "git/refs/heads/" + urllib.parse.quote(branch, safe="/"), method="PATCH", body={
        "sha": repaired["sha"], "force": False,
    })
    print(f"Repaired two target runtime locks on PR #{pull['number']}: {repaired['sha']}")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write(f"Updated architecture-specific runtime locks on PR #{pull['number']}. Required CI must validate the new commit.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
