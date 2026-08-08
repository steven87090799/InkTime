import pytest

from scripts.ci.provenance import (
    build_provenance,
    expected_tested_ref_kind,
    provenance_errors,
)


def _provenance(**overrides):
    value = {
        "event_name": "pull_request",
        "ref": "refs/pull/64/merge",
        "source_head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "tested_sha": "c" * 40,
        "tested_ref": "refs/pull/64/merge",
        "tested_ref_kind": "merge-ref",
    }
    value.update(overrides)
    return value


def test_pull_request_heavy_validation_is_merge_ref():
    assert expected_tested_ref_kind("pull_request", "refs/pull/64/merge") == "merge-ref"
    assert provenance_errors(_provenance()) == []


@pytest.mark.parametrize(
    ("event_name", "ref", "kind"),
    [
        ("push", "refs/heads/main", "main"),
        ("workflow_dispatch", "refs/heads/ci", "head"),
    ],
)
def test_non_pull_request_provenance_uses_tested_source_head(event_name, ref, kind):
    sha = "d" * 40
    value = build_provenance(
        event_name=event_name,
        ref=ref,
        source_head_sha=sha,
        base_sha="e" * 40,
        tested_sha=sha,
        tested_ref=ref,
        tested_ref_kind=kind,
    )
    assert provenance_errors(value) == []


def test_merge_ref_provenance_does_not_claim_source_head_equality():
    assert _provenance()["source_head_sha"] != _provenance()["tested_sha"]
    assert provenance_errors(_provenance()) == []


def test_wrong_ref_kind_is_rejected():
    errors = provenance_errors(_provenance(tested_ref_kind="head"))
    assert any("tested_ref_kind=head" in error for error in errors)


def test_merge_ref_requires_actual_pull_merge_ref():
    errors = provenance_errors(_provenance(tested_ref="refs/heads/ci/tiered-impact-test-planner"))
    assert any("refs/pull/<n>/merge" in error for error in errors)


def test_merge_ref_must_match_workflow_ref():
    errors = provenance_errors(_provenance(tested_ref="refs/pull/65/merge"))
    assert any("tested_ref must equal workflow ref" in error for error in errors)


def test_tested_ref_is_required():
    errors = provenance_errors(_provenance(tested_ref=""))
    assert any("tested_ref must identify" in error for error in errors)
