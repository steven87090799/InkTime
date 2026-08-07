# InkTime CI policy

InkTime CI uses the source-owned planner in [`scripts/ci/test_plan.py`](../scripts/ci/test_plan.py). The workflows only execute the suites and gates selected by that planner; the compatibility adapter [`scripts/ci/changed_paths.py`](../scripts/ci/changed_paths.py) remains for older boolean consumers.

## Impact mode and full mode

Draft pull requests without the `full-ci` label use impact mode. The planner classifies changed paths into production domains, owner suites, and expensive gates, then selects the smallest affected validation set. Unknown repository paths fail open to full mode, and a production domain without an owner suite also fails open.

Full mode is selected when any of these are true:

- a pull request is ready for review (`draft == false`);
- the pull request has the `full-ci` label;
- `workflow_dispatch` uses `full_suite=true`;
- a push targets `refs/heads/main`.

Full mode includes Tier 0, the complete owner-suite plan, Python 3.12 coverage at 80%, Python 3.10 compatibility, migration, dependency audit, runtime soak, Playwright, Docker LAN persistence, TLS smoke, firmware host contracts and the complete firmware profile matrix, container security, benchmark, and both aggregate gates. Equivalent impact-only heavy jobs are not run again in full mode.

## Validation tiers

- **Tier 0:** changed-path classification, planner contracts, secret scan, Ruff, and mypy. Dependency-policy validation runs when dependency tooling is relevant. Actionlint runs when CI/workflow configuration is relevant.
- **Tier 1/2:** source-owned Python, web, authentication, runtime, queue, persistence, migration, backup/restore, device, rendering, scanner, notification, settings, provider, Docker, TLS, firmware, and benchmark owner suites.
- **Tier 3:** only affected expensive gates run in impact mode. Firmware uses the PhotoPainter release as the quick profile and expands to affected profiles for shared firmware surfaces.
- **Tier 4:** full mode runs the complete pre-merge validation set and preserves the existing global coverage threshold.

Production changes select their owning regression boundaries even when the corresponding test file did not change. For example, scheduler changes route runtime soak, migration changes route persistence and migration owners, device manifest/ACK changes route device and firmware host contracts, and authentication/session changes route security, browser, and TLS boundaries.

## Routing and debugging

Both [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and [`.github/workflows/container-security.yml`](../.github/workflows/container-security.yml) emit the compact plan in the job summary, including mode, selected suites, selected gates, skipped gates, firmware profiles, and the no-duplicate invariant. To inspect a plan locally without running heavy validation:

```bash
python3 scripts/ci/test_plan.py --help
python3 scripts/ci/test_plan.py --event-name pull_request --ref refs/pull/1/merge --draft true README.md
```

Check `ci_mode`, `unknown_paths`, `owner_suite_gaps`, `selected_test_suites`, `selected_gates`, `full_plan_complete`, and `no_heavy_impact_duplicates`. A selected job failure or cancellation fails its aggregate gate; an intentionally skipped conditional job is accepted by the aggregate gate. A full run must be judged only from the exact pushed HEAD, not an older successful run.

The full suite is not run on every Draft push because Draft validation is intended to give fast, affected feedback while retaining secret, quality, routing, and relevant production-boundary checks. Use `full-ci`, mark the PR ready, dispatch `full_suite=true`, or push to `main` when the complete pre-merge set is required.
