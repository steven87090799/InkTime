import io
import json
from pathlib import Path
import zipfile

import pytest
import yaml

from scripts.ci import repair_dependency_locks, reuse_validation
from scripts.ci.repair_dependency_locks import eligible_pull, validate_requirements
from scripts.ci.summarize_test_timings import summarize


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("changes,expected", [
    ({"title": {}}, True), ({"body": {}}, True),
    ({"title": {}, "body": {}}, True), ({}, False),
    ({"base": {}}, False), ({"title": {}, "base": {}}, False),
    ({"unexpected": {}}, False), (None, False),
])
def test_only_known_title_body_edits_can_reuse(changes, expected):
    event = {"action": "edited", "changes": changes}
    assert reuse_validation.metadata_only(event, "pull_request") is expected
    assert not reuse_validation.metadata_only(event, "push")
    assert not reuse_validation.metadata_only({**event, "action": "synchronize"}, "pull_request")


def _plan():
    return {
        "event_name": "pull_request", "ref": "refs/pull/1/merge",
        "source_head_sha": "a" * 40, "base_sha": "b" * 40, "tested_sha": "c" * 40,
        "tested_ref": "refs/pull/1/merge", "tested_ref_kind": "merge-ref", "ci_mode": "impact",
        "selected_test_suites": ["ci_routing_contracts"], "selected_gates": ["actionlint"],
    }


@pytest.mark.parametrize("field", reuse_validation.EVIDENCE_FIELDS)
def test_reuse_rejects_every_changed_or_missing_evidence_field(field):
    plan = _plan()
    assert reuse_validation.evidence_matches(plan, plan)
    assert not reuse_validation.evidence_matches({**plan, field: "different"}, plan)
    missing = dict(plan)
    missing.pop(field)
    assert not reuse_validation.evidence_matches(missing, plan)


def test_legacy_provenance_or_unexpected_json_is_not_validation_evidence():
    assert not reuse_validation.evidence_matches({"tested_sha": "c" * 40}, _plan())
    assert not reuse_validation.evidence_matches([], _plan())


@pytest.mark.parametrize("status,conclusion,expected", [
    ("completed", "success", True), ("completed", "failure", False),
    ("completed", "cancelled", False), ("in_progress", None, False),
])
def test_hosted_reuse_requires_successful_prior_run(monkeypatch, status, conclusion, expected):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "20")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as fixture:
        fixture.writestr("ci-provenance-19.json", json.dumps(_plan()))

    def fake_api(path, *, archive=False):
        if "/workflows/" in path:
            return {"workflow_runs": [{"id": 19, "status": status, "conclusion": conclusion,
                                       "html_url": "https://github.com/owner/repo/actions/runs/19"}]}
        if path.endswith("/zip"):
            return fixture_bytes
        return {"artifacts": [{"name": "inktime-ci-provenance-19", "id": 1,
                               "expired": False, "size_in_bytes": 500}]}

    fixture_bytes = archive.getvalue()
    monkeypatch.setattr(reuse_validation, "api", fake_api)
    assert bool(reuse_validation.find_evidence(_plan(), "ci.yml", "ci")) is expected


def test_missing_api_evidence_falls_back_to_validation(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "edited", "changes": {"body": {}}}))
    output = tmp_path / "output"
    for name, value in {
        "GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_OUTPUT": str(output), "PLAN_JSON": json.dumps(_plan()),
        "VALIDATION_WORKFLOW": "ci.yml", "PROVENANCE_PREFIX": "ci",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(reuse_validation, "find_evidence", lambda *args: (_ for _ in ()).throw(OSError()))
    assert reuse_validation.main() == 0
    assert "full_validation=true" in output.read_text()
    assert "reused_run_id=\n" in output.read_text()


def test_successful_metadata_reuse_preserves_a_reviewable_run_link(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "edited", "changes": {"title": {}}}))
    output, summary = tmp_path / "output", tmp_path / "summary"
    for name, value in {
        "GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
        "PLAN_JSON": json.dumps(_plan()), "VALIDATION_WORKFLOW": "ci.yml",
        "PROVENANCE_PREFIX": "ci",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(reuse_validation, "find_evidence", lambda *args: {
        "run_id": 19, "url": "https://github.com/owner/repo/actions/runs/19",
    })
    assert reuse_validation.main() == 0
    assert "full_validation=false" in output.read_text()
    assert "reused_run_id=19" in output.read_text()
    assert "https://github.com/owner/repo/actions/runs/19" in summary.read_text()


def _pull():
    return {
        "state": "open", "user": {"login": "dependabot[bot]"},
        "head": {"ref": "dependabot/pip/example", "sha": "a" * 40, "repo": {"full_name": "owner/repo"}},
        "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
    }


def test_lock_repair_rejects_forks_stale_heads_and_human_prs():
    pull = _pull()
    assert eligible_pull(pull, "owner/repo", "dependabot/pip/example", "a" * 40)
    assert not eligible_pull(pull, "owner/repo", "dependabot/pip/example", "b" * 40)
    pull["head"]["repo"]["full_name"] = "fork/repo"
    assert not eligible_pull(pull, "owner/repo", "dependabot/pip/example", "a" * 40)
    pull = _pull()
    pull["user"]["login"] = "human"
    assert not eligible_pull(pull, "owner/repo", "dependabot/pip/example", "a" * 40)


@pytest.mark.parametrize("raced", [False, True])
def test_lock_repair_commits_only_locks_and_never_updates_a_changed_pr(monkeypatch, tmp_path, raced):
    pull = _pull()
    pull["number"] = 1
    pull["base"]["sha"] = "b" * 40
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"workflow_run": {
        "head_branch": "dependabot/pip/example", "head_sha": "a" * 40,
    }}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    writes = []

    def fake_api(repository, path, *, method="GET", body=None):
        if path.startswith("pulls?"):
            return [pull]
        if path == "pulls/1":
            return {**pull, "head": {**pull["head"], "sha": "d" * 40}} if raced else pull
        if path.startswith("git/commits/"):
            return {"tree": {"sha": "e" * 40}}
        writes.append((path, method, body))
        return {"sha": "f" * 40}

    def fake_file(repository, path, sha):
        if path == "requirements.txt":
            return "Pillow==1.0\n" if sha == "a" * 40 else "Pillow==0.9\n"
        return "Pillow==0.9 --hash=sha256:" + "a" * 64 + "\n"

    def generate(argv, *, check):
        output = Path(argv[argv.index("--output") + 1])
        for path in repair_dependency_locks.LOCK_PATHS:
            (output / Path(path).name).write_text("Pillow==1.0 --hash=sha256:" + "b" * 64 + "\n")

    monkeypatch.setattr(repair_dependency_locks, "api", fake_api)
    monkeypatch.setattr(repair_dependency_locks, "file_text", fake_file)
    monkeypatch.setattr(repair_dependency_locks.subprocess, "run", generate)
    assert repair_dependency_locks.main() == 0
    if raced:
        assert writes == []
    else:
        tree = next(body for path, method, body in writes if path == "git/trees")
        assert {item["path"] for item in tree["tree"]} == set(repair_dependency_locks.LOCK_PATHS)
        commit = next(body for path, method, body in writes if path == "git/commits")
        assert commit["parents"] == ["a" * 40]
        ref = next(body for path, method, body in writes if path.startswith("git/refs/"))
        assert ref["force"] is False
@pytest.mark.parametrize("invalid", [
    "--index-url https://example.invalid", "package @ https://example.invalid/file.whl",
    "-e .", "-r another.txt", "", "package==1\nPackage==2",
])
def test_lock_repair_never_executes_requirement_options_or_project_code(invalid):
    with pytest.raises(ValueError):
        validate_requirements(invalid)
    validate_requirements("# comment\nPillow==12.3.0\npillow-heif==1.8.0\n")


def test_timings_rank_cases_and_accumulate_modules(tmp_path):
    report = tmp_path / "timings.xml"
    report.write_text('<testsuites><testsuite>'
                      '<testcase classname="tests.fast" name="one" time="1"/>'
                      '<testcase classname="tests.slow" name="two" time="4"/>'
                      '<testcase classname="tests.slow" name="three" time="3"/>'
                      '</testsuite></testsuites>')
    summary = summarize(report, limit=1)
    assert "3 cases; 8.0s" in summary
    assert "tests.slow::two" in summary
    assert "tests.slow::three" not in summary
    assert "| 7.00 | `tests.slow` |" in summary


def test_privileged_lock_repair_runs_only_default_branch_code():
    workflow = yaml.safe_load((ROOT / ".github/workflows/dependency-lock-repair.yml").read_text())
    assert workflow["on"] == {"workflow_run": {"workflows": ["InkTime CI"], "types": ["completed"]}}
    job = workflow["jobs"]["repair"]
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False
    assert "head_repository.full_name == github.repository" in job["if"]
    assert "pull_request_target" not in workflow["on"]
    assert workflow["permissions"] == {"contents": "write", "pull-requests": "read"}


def test_full_suite_retains_timings_on_failure():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["python-quality"]["steps"]
    test_step = next(step for step in steps if step.get("name", "").startswith("Python 3.12 full"))
    assert "--durations=30" in test_step["run"]
    assert "--junitxml=artifacts/python-full.xml" in test_step["run"]
    upload = next(step for step in steps if step.get("name") == "Upload full-suite timings")
    assert "always()" in upload["if"]
    assert "artifacts/python-full.xml" in upload["with"]["path"]
