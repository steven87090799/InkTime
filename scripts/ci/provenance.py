"""Build and validate truthful GitHub Actions source/test provenance."""

from __future__ import annotations

import argparse
import json
import re

COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
PR_MERGE_REF = re.compile(r"^refs/pull/[1-9][0-9]*/merge$")
TESTED_REF_KINDS = frozenset({"merge-ref", "head", "main"})


def expected_tested_ref_kind(event_name: str, ref: str) -> str:
    if event_name == "pull_request":
        return "merge-ref"
    if event_name == "push" and ref == "refs/heads/main":
        return "main"
    return "head"


def build_provenance(
    *,
    event_name: str,
    ref: str,
    source_head_sha: str,
    base_sha: str,
    tested_sha: str,
    tested_ref: str,
    tested_ref_kind: str,
) -> dict[str, str]:
    return {
        "event_name": event_name,
        "ref": ref,
        "source_head_sha": source_head_sha,
        "base_sha": base_sha,
        "tested_sha": tested_sha,
        "tested_ref": tested_ref,
        "tested_ref_kind": tested_ref_kind,
    }


def provenance_errors(provenance: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for field in (
        "event_name",
        "ref",
        "source_head_sha",
        "base_sha",
        "tested_sha",
        "tested_ref",
        "tested_ref_kind",
    ):
        if not str(provenance.get(field, "")).strip():
            errors.append(f"{field} must not be empty")
    for field in ("source_head_sha", "base_sha", "tested_sha"):
        value = provenance.get(field, "")
        if COMMIT_SHA.fullmatch(value) is None:
            errors.append(f"{field} must be a complete 40-character commit SHA")

    tested_ref = provenance.get("tested_ref", "")
    if not tested_ref:
        errors.append("tested_ref must identify the checkout ref that was validated")

    tested_ref_kind = provenance.get("tested_ref_kind", "")
    if tested_ref_kind not in TESTED_REF_KINDS:
        errors.append(
            "tested_ref_kind must be one of merge-ref, head, or main "
            f"(actual={tested_ref_kind or 'missing'})"
        )

    event_name = provenance.get("event_name", "")
    ref = provenance.get("ref", "")
    expected_kind = expected_tested_ref_kind(event_name, ref)
    if tested_ref_kind != expected_kind:
        errors.append(
            f"tested_ref_kind={tested_ref_kind or 'missing'} does not match "
            f"event={event_name or 'missing'} ref={ref or 'missing'} "
            f"(expected={expected_kind})"
        )

    if tested_ref_kind == "merge-ref":
        if PR_MERGE_REF.fullmatch(tested_ref) is None:
            errors.append(f"merge-ref validation requires refs/pull/<n>/merge (actual={tested_ref})")
        if tested_ref != ref:
            errors.append(
                f"merge-ref tested_ref must equal workflow ref (tested_ref={tested_ref} ref={ref})"
            )
    elif tested_ref_kind in {"head", "main"} and event_name != "pull_request":
        if tested_ref != ref:
            errors.append(
                f"{tested_ref_kind} tested_ref must equal workflow ref "
                f"(tested_ref={tested_ref} ref={ref})"
            )

    if event_name != "pull_request" and provenance.get("source_head_sha") != provenance.get(
        "tested_sha"
    ):
        errors.append(
            "non-pull_request source_head_sha must equal tested_sha "
            f"(source={provenance.get('source_head_sha', 'missing')} "
            f"tested={provenance.get('tested_sha', 'missing')})"
        )
    return errors


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--source-head-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--tested-sha", required=True)
    parser.add_argument("--tested-ref", required=True)
    parser.add_argument("--tested-ref-kind", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    provenance = build_provenance(
        event_name=args.event_name,
        ref=args.ref,
        source_head_sha=args.source_head_sha,
        base_sha=args.base_sha,
        tested_sha=args.tested_sha,
        tested_ref=args.tested_ref,
        tested_ref_kind=args.tested_ref_kind,
    )
    errors = provenance_errors(provenance)
    if errors:
        print("\n".join(errors))
        return 1
    print(json.dumps(provenance, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
