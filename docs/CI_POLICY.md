# InkTime CI policy

Routine local validation and delivery must follow [AGENTS.md](../AGENTS.md): static checks only, inspect resulting Actions once after a push, report `CI_PENDING` for queued/running jobs, and keep PRs Draft. Commands below describe the planner, not permission to run hosted-only suites locally.

InkTime CI uses the existing source-owned path/domain planner in [`scripts/ci/test_plan.py`](../scripts/ci/test_plan.py), with [`scripts/ci/canonical_plan.py`](../scripts/ci/canonical_plan.py) as the workflow-facing canonical output contract. `canonical_plan.py` does not duplicate path routing: it delegates ownership and tier selection to `test_plan.py`, then adds planner-wide invariants and provenance metadata. The workflows only execute suites and gates selected by that canonical output. Tier 1/2 owner suites are executed by [`scripts/ci/run_selected_suites.py`](../scripts/ci/run_selected_suites.py), while heavy gates retain their dedicated jobs. The compatibility adapter [`scripts/ci/changed_paths.py`](../scripts/ci/changed_paths.py) remains for older boolean consumers.

## Impact mode and full mode

Draft pull requests without the `full-ci` label use impact mode. The planner classifies changed paths into production domains, owner suites, and expensive gates, then selects the smallest affected validation set. Unknown repository paths fail open to full mode, and a production domain without an owner suite also fails open.

Full mode is selected when any of these are true:

- a triggered pull request event reports that the pull request is ready for review (`draft == false`);
- the pull request has the `full-ci` label;
- `workflow_dispatch` uses `full_suite=true`;
- a push targets `refs/heads/main`.

Full mode includes Tier 0, the complete owner-suite plan, Python 3.12 coverage at 80%, Python 3.10 compatibility, dependency policy and audit, migrations, secret scan, actionlint, Docker LAN production persistence, TLS production smoke, bounded runtime soak, Playwright, firmware host contracts and the complete firmware profile matrix, container security, offline benchmark, and both aggregate gates. Equivalent impact-only heavy jobs are not run again in full mode. Actionlint is a full-mode invariant even when the diff itself is not a workflow/configuration change.

The planner's full-mode execution registry maps every `FULL_PLAN_SUITES` entry to a real full-mode job. `docs_contract` is a documentation classification marker and is intentionally non-executable.

Cross-layer integration regressions are mapped to their domain owner in [`scripts/ci/test_plan.py`](../scripts/ci/test_plan.py) and [`scripts/ci/run_selected_suites.py`](../scripts/ci/run_selected_suites.py); there is no catch-all integration-directory runner. The explicit full-only allowlist is reserved for the multi-domain scheduled-release pipeline and records its reason in source.

## Execution attestation and fail-closed aggregate gates

[`scripts/ci/verify_execution.py`](../scripts/ci/verify_execution.py) is the shared source-owned execution-attestation contract for both aggregate workflows. The planner decides what is selected; the verifier maps selected suites/gates to their workflow execution owner and compares that expectation with GitHub Actions `toJSON(needs)`.

The rule is fail-closed:

- a planner-selected execution must exist in the current workflow's `needs` and its result must be `success`;
- selected + `skipped`, `failure`, or `cancelled` fails the aggregate gate;
- a selected suite/gate with no known execution owner fails the aggregate gate;
- an unselected job may be `skipped` without failing impact mode;
- full mode requires `full_plan_complete=true` and all applicable full execution owners to succeed;
- `selected-owner-suites` is intentionally impact-only and may be skipped in full mode because full jobs own the same regression boundaries there.

The repository and container aggregate gates both call the same verifier. They do not reimplement routing decisions in YAML.

## PR source HEAD versus tested merge-ref

A `pull_request` workflow normally checks out GitHub's PR merge ref, not the source branch commit itself. InkTime keeps that behavior because validating the prospective merge result is useful pre-merge evidence. It must not be described as exact source-head validation.

Every planner summary records these values explicitly:

- `SOURCE_HEAD_SHA`: the PR source branch commit being planned;
- `BASE_SHA`: the PR base commit used for changed-path classification;
- `TESTED_SHA`: `git rev-parse HEAD` from the actual checkout being validated;
- `TESTED_REF`: the GitHub ref of that checkout;
- `TESTED_REF_KIND`: `merge-ref`, `head`, or `main`.

For `pull_request` runs, the expensive validation jobs remain on the merge-ref and therefore normally report `TESTED_REF_KIND=merge-ref`. The repository workflow also runs a lightweight `source-head-contract` job with:

```yaml
ref: ${{ github.event.pull_request.head.sha }}
```

That job proves the checked-out commit equals `SOURCE_HEAD_SHA`. It does not duplicate the expensive full suite. Final pre-merge review must use the latest source HEAD and the full PR merge-ref validation generated for that source HEAD, never an older successful run.

## Validation tiers

- **Tier 0:** changed-path classification, planner contracts, secret scan, Ruff, mypy, dependency policy, and patch-format validation. Actionlint runs for CI/workflow configuration in impact mode and for every full run.
- **Tier 1/2:** source-owned Python, web, authentication, runtime, queue, persistence, migration, backup/restore, device, rendering, scanner, notification, settings, provider, Docker, TLS, firmware, and benchmark owner suites.
- **Tier 3:** only affected expensive gates run in impact mode. Firmware uses the PhotoPainter release as the quick profile and expands to affected profiles for shared firmware surfaces.
- **Tier 4:** full mode runs the complete pre-merge validation set and preserves the existing global coverage threshold.

Production changes select their owning regression boundaries even when the corresponding test file did not change. Scheduler changes route runtime soak; migration changes route persistence and migration owners; device manifest/ACK changes route device and firmware host contracts; authentication/session changes route security, browser, and TLS boundaries.

Provider/analysis impact coverage includes the direct cross-layer regressions in `test_analysis_pipeline.py`, `test_ai_cache_singleflight.py`, and `test_photo_quality_ai.py`. Render/release impact coverage includes `test_adaptive_frame_renderer.py`, `test_dual_photo_caption_layout.py`, and `test_render_candidate_contract.py`. Other owner mappings remain focused rather than indiscriminately running all of `tests/integration`. If an integration regression is intentionally full-only, it must be listed in `FULL_ONLY_INTEGRATION_TESTS` with a non-empty reason instead of being omitted accidentally.

The deployment preflight script is a cross-boundary surface: `scripts/production_preflight.py` routes to both TLS smoke and Docker LAN persistence because the LAN job invokes its `--mode lan` path, while `scripts/production_tls_smoke.py` remains TLS-only. Test-only backup/restore changes use the focused selected-suite runner, while production backup/restore changes retain the Docker LAN gate.

`inktime/app/platform.py` is the central session, CSRF, access-control, secure-cookie, proxy, and HSTS boundary, so it routes to authentication/security and TLS ownership without automatically starting Playwright. Every selected-runner mapping is resolved from the repository root and must point to an existing `test_*.py` file or a directory containing one; runtime validation remains fail-closed.

## Python support policy

`pyproject.toml` is a Python 3.10/package-metadata contract in addition to a development-tooling surface. Dependency policy parses `[project].requires-python` with `packaging.specifiers.SpecifierSet` and verifies that `packaging.version.Version("3.10")` is actually accepted. Text-prefix checks such as `startswith(">=3.10")` are not sufficient because exclusions and contradictory upper bounds can invalidate Python 3.10 support. `packaging` is an exact-pinned development dependency under the repository dependency policy.

## Routing and debugging

Both [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and [`.github/workflows/container-security.yml`](../.github/workflows/container-security.yml) emit the compact canonical plan and validation provenance in the job summary. To inspect a plan without running heavy validation:

The summaries include mode, selected suites, selected gates, skipped gates, firmware profiles, no-duplicate invariant, and truthful provenance fields: `SOURCE_HEAD_SHA`, `BASE_SHA`, `TESTED_SHA`, `TESTED_REF`, and `TESTED_REF_KIND`. Pull-request heavy jobs intentionally validate the GitHub merge ref (`TESTED_REF_KIND=merge-ref`); the lightweight source-head contract checks `github.event.pull_request.head.sha` separately. [`scripts/ci/verify_execution.py`](../scripts/ci/verify_execution.py) is the shared source-owned aggregate verifier: every planner-selected execution must report `success`, while an unselected conditional job may report `skipped`.

```bash
python3 scripts/ci/canonical_plan.py --help
python3 scripts/ci/canonical_plan.py --event-name pull_request --ref refs/pull/1/merge --draft true README.md
```

Check `ci_mode`, `unknown_paths`, `owner_suite_gaps`, `full_only_test_paths`, `selected_test_suites`, `selected_owner_suites`, `selected_gates`, `skipped_gates`, `suite_execution_gaps`, `full_suite_execution_gaps`, `full_plan_complete`, `no_heavy_impact_duplicates`, `requires_source_head_contract`, and provenance. A selected job that is skipped, failed, cancelled, missing, or unknown fails its aggregate gate with the execution ID and job name; only unselected skipped jobs are accepted. A full PR run is merge-ref validation and must be judged from the current PR event and its reported provenance, not described as direct source-head execution.

The full suite is not run on every Draft push because Draft validation is intended to give fast, affected feedback while retaining secret, quality, routing, and relevant production-boundary checks. Full mode is available through those existing triggers; routine agents must still follow `AGENTS.md` and must not dispatch, rerun or poll to manufacture a green result. Ready conversion, merges and pushes to protected branches require the applicable authorization.

Every source commit pushed to a pull request branch triggers validation through `synchronize`. Changing a Draft pull request to Ready for review triggers both workflows through `ready_for_review`; because the pull request is then ready (`draft == false`), the planner selects full mode for the unchanged source HEAD and refreshes both required aggregate gates. This avoids a ruleset deadlock where successful Draft checks remain visible but GitHub keeps the Ready pull request in `BLOCKED` with no bypass actor. Base retargets remain covered by `edited`, while title/body-only edits stay on the metadata lane. Required aggregate gates, strict branch protection, and fail-closed revalidation remain unchanged.
