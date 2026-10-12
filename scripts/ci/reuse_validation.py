"""Reuse successful hosted validation only for identical PR title/body edits."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import zipfile

EVIDENCE_FIELDS = (
    "event_name", "ref", "source_head_sha", "base_sha", "tested_sha",
    "tested_ref", "tested_ref_kind", "ci_mode", "selected_test_suites",
    "selected_gates",
)
LIMIT = 2 * 1024 * 1024


def metadata_only(event: dict, event_name: str) -> bool:
    changes = event.get("changes")
    return (
        event_name == "pull_request" and event.get("action") == "edited"
        and isinstance(changes, dict) and bool(changes)
        and set(changes) <= {"title", "body"}
    )


def evidence_matches(candidate: dict, current: dict) -> bool:
    return (
        isinstance(candidate, dict) and isinstance(current, dict)
        and current.get("tested_ref_kind") == "merge-ref"
        and all(field in candidate and field in current for field in EVIDENCE_FIELDS)
        and all(candidate[field] == current[field] for field in EVIDENCE_FIELDS)
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(path: str, *, archive: bool = False):
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    # Artifact redirects must never forward the API credential to storage.
    opener = urllib.request.build_opener(NoRedirect)
    try:
        response = opener.open(request, timeout=15)  # noqa: S310 -- fixed GitHub API origin
    except urllib.error.HTTPError as exc:
        if not archive or exc.code not in {301, 302, 303, 307, 308}:
            raise
        location = exc.headers["Location"]
        if urllib.parse.urlsplit(location).scheme != "https":
            raise ValueError("Artifact download must use HTTPS") from exc
        response = urllib.request.urlopen(location, timeout=15)  # noqa: S310 -- HTTPS, no credentials
    with response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("Validation evidence exceeds size limit")
    return data if archive else json.loads(data)


def find_evidence(plan: dict, workflow: str, prefix: str) -> dict | None:
    repository = os.environ["GITHUB_REPOSITORY"]
    query = urllib.parse.urlencode({
        "event": "pull_request", "head_sha": plan["source_head_sha"], "per_page": 30,
    })
    runs = api(f"/repos/{repository}/actions/workflows/{workflow}/runs?{query}")
    for run in runs["workflow_runs"][:5]:
        if str(run["id"]) == os.environ["GITHUB_RUN_ID"]:
            continue
        if run["status"] != "completed" or run["conclusion"] != "success":
            continue
        artifacts = api(f"/repos/{repository}/actions/runs/{run['id']}/artifacts?per_page=100")
        for artifact in artifacts["artifacts"]:
            if artifact["name"] != f"inktime-{prefix}-provenance-{run['id']}" or artifact["expired"]:
                continue
            if artifact["size_in_bytes"] > LIMIT:
                continue
            raw = api(f"/repos/{repository}/actions/artifacts/{artifact['id']}/zip", archive=True)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = archive.infolist()
                if len(members) != 1 or members[0].file_size > LIMIT:
                    continue
                candidate = json.loads(archive.read(members[0]))
            if evidence_matches(candidate, plan):
                return {"run_id": run["id"], "url": run["html_url"]}
    return None


def main() -> int:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    reused = None
    if metadata_only(event, os.environ["GITHUB_EVENT_NAME"]):
        try:
            reused = find_evidence(
                json.loads(os.environ["PLAN_JSON"]), os.environ["VALIDATION_WORKFLOW"],
                os.environ["PROVENANCE_PREFIX"],
            )
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            print("No verifiable hosted evidence available; running fresh validation.")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"full_validation={'false' if reused else 'true'}\n")
        output.write(f"reused_run_id={reused['run_id'] if reused else ''}\n")
    if reused:
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(
                f"\nTitle/body edit: reused [successful run {reused['run_id']}]({reused['url']}) "
                "with identical source, base, merge checkout and selected validation plan.\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
