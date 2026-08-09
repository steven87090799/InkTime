from __future__ import annotations

from pathlib import Path


STACKED_BASE = "fix/quiet-runtime-offline-playlist"


def test_pr65_stacked_base_routes_both_hosted_workflows():
    root = Path(__file__).parents[2]
    for relative in (
        ".github/workflows/ci.yml",
        ".github/workflows/container-security.yml",
    ):
        workflow = (root / relative).read_text(encoding="utf-8")
        pull_request = workflow.split("pull_request:", 1)[1].split("push:", 1)[0]
        assert f"- {STACKED_BASE}" in pull_request
        assert "types: [opened, synchronize, reopened, ready_for_review, labeled, unlabeled, edited]" in pull_request
