# InkTime CI policy

Routine local validation and delivery must follow [AGENTS.md](../AGENTS.md): static checks only, inspect resulting Actions once after a push, report `CI_PENDING` for queued/running jobs, and keep PRs Draft. Commands below describe the planner, not permission to run hosted-only suites locally.

InkTime CI uses the existing source-owned path/domain planner in [`scripts/ci/test_plan.py`](../scripts/ci/test_plan.py), with [`scripts/ci/canonical_plan.py`](../scripts/ci/canonical_plan.py) as the workflow-facing canonical output contract. `canonical_plan.py` does not duplicate path routing: it delegates ownership and tier selection to `test_plan.py`, then adds planner-wide invariants and provenance metadata. The workflows only execute suites and gates selected by that canonical output. Tier 1/2 owner suites are executed by [`scripts/ci/run_selected_suites.py`](../scripts/ci/run_selected_suites.py), while heavy gates retain their dedicated jobs. The compatibility adapter [`scripts/ci/changed_paths.py`](../scripts/ci/changed_paths.py) remains for older boolean consumers.

## Impact mode and full mode

Pull requests (including Ready pull requests) and main pushes use impact mode by default. Main pushes classify the actual pushed diff using the event's before SHA. The planner selects affected production domains, owner suites, and expensive gates. Unknown repository paths and a production domain without an owner suite still escalate to full mode.

Full mode is selected when any of these are true:

- the pull request has the `full-ci` label;
- `workflow_dispatch` uses `full_suite=true` (the default is false).

Use the `full-ci` label before release or select `full_suite=true` in **both**
the CI and Container Security workflows when manually requesting complete
validation. The global Python 3.12 coverage threshold remains in full mode.
There is no separate Python 3.10 compatibility run. Routine main pushes no longer repeat
that complete plan automatically. Unknown-path, owner-gap and explicit
full-only regression fallbacks remain conservative.

Full mode includes Tier 0, the complete owner-suite plan, Python 3.12 coverage at 80%, dependency policy and audit, migrations, secret scan, actionlint, Docker LAN production persistence, TLS production smoke, bounded runtime soak, Playwright, firmware host contracts and the deployed PhotoPainter release, container security, offline benchmark, and both aggregate gates. Equivalent impact-only heavy jobs are not run again in full mode. Actionlint is a full-mode invariant even when the diff itself is not a workflow/configuration change.

The planner's full-mode execution registry maps every `FULL_PLAN_SUITES` entry to a real full-mode job. `docs_contract` remains a documentation classification marker for routing, while each full-validation `changes` job runs [`scripts/ci/validate_ai_navigation.py`](../scripts/ci/validate_ai_navigation.py) after checkout. That contract validates the machine-readable AI index, its paths and test globs, the large-file policy, and the required navigation links.

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

- **Tier 0:** changed-path classification, planner contracts, secret scan, and patch-format validation. Ruff and mypy are added for Python/configuration changes and always run in full mode. Dependency policy is required for dependency changes and full mode. Actionlint runs for CI/workflow configuration in impact mode and for every full run.
- **Tier 1/2:** source-owned Python, web, authentication, runtime, queue, persistence, migration, backup/restore, device, rendering, scanner, notification, settings, provider, Docker, TLS, firmware, and benchmark owner suites.
- **Tier 3:** only affected expensive gates run in impact mode. Firmware compiles only the deployed PhotoPainter release, including when shared firmware surfaces change.
- **Tier 4:** full mode runs the complete pre-merge validation set and preserves the existing global coverage threshold.

The 100,000-row cases marked `performance` are excluded from the ordinary
unit/compatibility commands (the 10,000-row regression remains in those
commands) and run by the scheduled performance workflow with an uploaded
report.

Production changes select their owning regression boundaries even when the corresponding test file did not change. Scheduler changes route runtime soak; migration changes route persistence and migration owners; device manifest/ACK changes route device and firmware host contracts; authentication/session changes route security, browser, and TLS boundaries.

Provider/analysis impact coverage includes the direct cross-layer regressions in `test_analysis_pipeline.py`, `test_ai_cache_singleflight.py`, and `test_photo_quality_ai.py`. Render/release impact coverage includes `test_adaptive_frame_renderer.py`, `test_dual_photo_caption_layout.py`, and `test_render_candidate_contract.py`. Other owner mappings remain focused rather than indiscriminately running all of `tests/integration`. If an integration regression is intentionally full-only, it must be listed in `FULL_ONLY_INTEGRATION_TESTS` with a non-empty reason instead of being omitted accidentally.

The deployment preflight script is a cross-boundary surface: `scripts/production_preflight.py` routes to both TLS smoke and Docker LAN persistence because the LAN job invokes its `--mode lan` path, while `scripts/production_tls_smoke.py` remains TLS-only. Test-only backup/restore changes use the focused selected-suite runner, while production backup/restore changes retain the Docker LAN gate.

`inktime/app/platform.py` is the central session, CSRF, access-control, secure-cookie, proxy, and HSTS boundary, so it routes to authentication/security and TLS ownership without automatically starting Playwright. Every selected-runner mapping is resolved from the repository root and must point to an existing `test_*.py` file or a directory containing one; runtime validation remains fail-closed.

## Python support policy

The deployed Docker images use Python 3.12. Package metadata now targets
`>=3.12,<3.13`, and Ruff/mypy also target 3.12. Dependency policy checks the
PEP 440 specifier semantically: it must accept 3.12 while excluding 3.11 and
3.13. There is no second complete test run for Python 3.10, even in full mode.
This narrows supported package installations to the deployed minor version;
other Python versions are no longer advertised or validated. Python 3.12
owner regressions and optional full coverage still protect deployed behavior.


## Routing and debugging

Both [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and [`.github/workflows/container-security.yml`](../.github/workflows/container-security.yml) emit the compact canonical plan and validation provenance in the job summary. To inspect a plan without running heavy validation:

The summaries include mode, selected suites, selected gates, skipped gates, firmware profiles, no-duplicate invariant, and truthful provenance fields: `SOURCE_HEAD_SHA`, `BASE_SHA`, `TESTED_SHA`, `TESTED_REF`, and `TESTED_REF_KIND`. Pull-request heavy jobs intentionally validate the GitHub merge ref (`TESTED_REF_KIND=merge-ref`); the lightweight source-head contract checks `github.event.pull_request.head.sha` separately. [`scripts/ci/verify_execution.py`](../scripts/ci/verify_execution.py) is the shared source-owned aggregate verifier: every planner-selected execution must report `success`, while an unselected conditional job may report `skipped`.

```bash
python3 scripts/ci/canonical_plan.py --help
python3 scripts/ci/canonical_plan.py --event-name pull_request --ref refs/pull/1/merge --draft true README.md
```

Check `ci_mode`, `unknown_paths`, `owner_suite_gaps`, `full_only_test_paths`, `selected_test_suites`, `selected_owner_suites`, `selected_gates`, `skipped_gates`, `suite_execution_gaps`, `full_suite_execution_gaps`, `full_plan_complete`, `no_heavy_impact_duplicates`, `requires_source_head_contract`, and provenance. A selected job that is skipped, failed, cancelled, missing, or unknown fails its aggregate gate with the execution ID and job name; only unselected skipped jobs are accepted. A full PR run is merge-ref validation and must be judged from the current PR event and its reported provenance, not described as direct source-head execution.

The full suite is not run on every pull-request event or main push because impact validation is intended to give fast, affected feedback while retaining secret, routing, static checks for changed Python surfaces, and relevant production-boundary checks. Full mode remains available through the explicit label, manual dispatch, and conservative fallback rules. Routine agents must still follow `AGENTS.md` and must not dispatch, rerun or poll to manufacture a green result.

## Execution time and optional long checks

In successful PR run [37963174961](https://github.com/steven87090799/InkTime/actions/runs/37963174961),
selected owner regressions ran 2,591 tests in 38m11s on a single runner.
The eight selected firmware profiles compiled sequentially in 14m31s, plus
about one minute of toolchain setup. These jobs ran concurrently; the Python
job determined the approximately 39-minute elapsed time. NAS update E2E,
TLS, Playwright and bounded soak each finished in roughly one to two minutes.
The log did not report individual test durations, so it does not identify
which fixtures or test cases consumed those 38 minutes.

The latest successful main run
[36381532512](https://github.com/steven87090799/InkTime/actions/runs/36381532512)
took 76m15s overall. Its Python 3.12 full-test/coverage job took 75m54s,
and Python 3.10 compatibility took 40m59s concurrently. These execute the
complete unit/security/integration set; coverage adds instrumentation. Their
per-test costs were not reported. Keeping this complete plan behind explicit
full validation removes substantial repeated work from routine main pushes;
an explicitly requested full run can still take this long.

The selected-suite runner now partitions whole test files across up to four
isolated runners. At most 20 selected files use one runner; broader selections
use up to four. Greedy balancing estimates workload from test-function counts,
with extra weight for integration/security files. It does not execute/import
tests while planning and is not a measured-runtime guarantee. A file belongs
to exactly one shard, so module fixtures retain their original scope. Every
shard reports its slowest 30 test phases and uploads JUnit timing/results.
Very large individual files can still dominate and should be tuned using
those hosted measurements.

Firmware CI now compiles exactly `photopainter_release`: Waveshare
PhotoPainter Rev2.0, ESP32-S3, 16 MiB Flash, 8 MiB OPI PSRAM, USB CDC, and the
repository's `inktime_photopainter_3M_16MB.csv` partition table. Both impact and
full planning have the same singleton profile inventory. GDEY/GDEP 4 MiB,
separate trusted-LAN builds and Debug builds are no longer compiled. The
PhotoPainter release already supports strictly checked private-IPv4 LAN HTTP
as well as trusted HTTPS, so its extra trusted-LAN variant was redundant.
The host Config/ACK/queue tests remain relevant; the duplicate default-board
PhotoPainter core run is removed. Source for other boards remains intact.

Selected owner tests still use a bounded matrix and fail-closed aggregate
attestation. Parallelism reduces elapsed time rather than computation;
removing seven unrelated firmware builds and the Python 3.10 full run also
reduces total runner work. The old full-run measurements above describe the
previous eight-profile/two-Python configuration, not the narrowed plan.


Long-duration runtime soak is already isolated in `runtime-soak.yml` and is
manual-only: 30 minutes, two hours (default), or five hours. It is not part of
ordinary PR CI. `nightly-performance.yml` runs the 100,000-row scale checks
on its weekly schedule or by manual dispatch; its 60-minute timeout is a
ceiling, not evidence that every run takes an hour. The short bounded soak
and small regressions remain selected when their owning production paths
change. Keep authentication, persistence, update preservation and affected
firmware compatibility checks rather than skipping them solely to show green.

The latest scheduled performance run
[37236630074](https://github.com/steven87090799/InkTime/actions/runs/37236630074)
failed in about a minute: the unchanged-scan regression observed zero cached
photos instead of 10,000/100,000. It is separate from the PR wait and does not
currently establish successful scale acceptance. Scanner/fixture diagnosis
is a separate repair from this CI scheduling change.

Every source commit pushed to a pull request branch triggers validation through `synchronize`. Changing a Draft pull request to Ready for review triggers both workflows through `ready_for_review`; the planner keeps the unchanged source HEAD on impact mode and refreshes both required aggregate gates without starting the full suite. A full pre-merge run can be requested with the `full-ci` label or `workflow_dispatch` with `full_suite=true`. Base retargets and title/body edits are covered by `edited` and run the same planner-selected impact validation. Every run publishes the fixed `Repository gate` and `Container security gate` identities after execution attestation. Metadata-only runs with alternate gate names left the required checks Expected on PR #132 despite earlier successful runs, so they are no longer emitted. The `full_validation` event output enables planning and attestation; it does not force the full test suite. Required aggregate gates, strict branch protection, and fail-closed revalidation remain unchanged.
