"""Canonical hosted-CI planner entrypoint.

``test_plan.py`` retains InkTime's existing path/domain/tier architecture.  This
module adds only planner-wide invariants and provenance fields that every
workflow must consume identically.  No routing policy lives in workflow YAML.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.test_plan import (
    FULL_GATE_EXECUTION,
    FULL_MODE,
    GATE_ORDER,
    build_test_plan,
)


def _ordered(values: Iterable[str]) -> list[str]:
    value_set = set(values)
    known = [value for value in GATE_ORDER if value in value_set]
    known_set = set(known)
    return known + sorted(value_set - known_set)


def build_canonical_plan(
    paths: Iterable[str], event_context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    """Return the single workflow-consumable CI plan.

    The existing planner remains authoritative for changed-path ownership and
    impact/full selection. Full validation additionally requires actionlint for
    every full run, independent of which path happened to trigger full mode.
    Provenance describes the source commit being planned; the workflow records
    the separately tested checkout SHA/ref at runtime.
    """

    context = event_context or {}
    plan = dict(build_test_plan(paths, context))
    event_name = str(context.get("event_name", "")).strip()

    if plan["ci_mode"] == FULL_MODE:
        selected_gates = _ordered([*plan["selected_gates"], "actionlint"])
        plan["selected_gates"] = selected_gates
        actionlint_execution = FULL_GATE_EXECUTION["actionlint"]
        selected_execution_ids = list(plan["selected_execution_ids"])
        if actionlint_execution not in selected_execution_ids:
            selected_execution_ids.append(actionlint_execution)
        full_execution_ids = list(plan["full_execution_ids"])
        if actionlint_execution not in full_execution_ids:
            full_execution_ids.append(actionlint_execution)
        plan["selected_execution_ids"] = selected_execution_ids
        plan["full_execution_ids"] = full_execution_ids
        plan["execution_id_duplicates"] = sorted(
            {
                execution_id
                for execution_id in selected_execution_ids
                if selected_execution_ids.count(execution_id) > 1
            }
        )
        plan["execution_overlap"] = sorted(
            set(plan["impact_execution_ids"]) & set(full_execution_ids)
        )
        plan["full_plan_complete"] = bool(
            plan["full_plan_complete"]
            and "actionlint" in selected_gates
            and not plan["execution_id_duplicates"]
            and not plan["execution_overlap"]
        )
        plan["no_heavy_impact_duplicates"] = bool(
            plan["no_heavy_impact_duplicates"]
            and not plan["execution_id_duplicates"]
            and not plan["execution_overlap"]
        )

    source_head_sha = context.get("head_sha")
    plan.update(
        {
            "event_name": event_name,
            "source_head_sha": None if source_head_sha in {None, ""} else str(source_head_sha),
            "validation_ref": str(context.get("ref", "")).strip() or None,
            "requires_source_head_contract": event_name == "pull_request",
        }
    )
    return plan


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="repository-relative changed paths")
    parser.add_argument("--paths-file", help="read one repository-relative path per line")
    parser.add_argument("--event-name", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha")
    parser.add_argument("--draft", choices=("true", "false"))
    parser.add_argument("--full-suite", action="store_true")
    parser.add_argument("--label", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    paths = list(args.paths)
    if args.paths_file:
        with open(args.paths_file, encoding="utf-8") as path_file:
            paths.extend(line.strip() for line in path_file if line.strip())

    context: dict[str, object] = {
        "event_name": args.event_name,
        "ref": args.ref,
        "base_sha": args.base_sha,
        "head_sha": args.head_sha,
        "full_suite": args.full_suite,
        "labels": args.label,
    }
    if args.draft is not None:
        context["draft"] = args.draft
    print(
        json.dumps(
            build_canonical_plan(paths, context),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
