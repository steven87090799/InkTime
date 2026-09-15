# InkTime AI Agent Rules

## Context: smallest useful evidence
Complete the requested task with the minimum evidence necessary for correctness.
Before each read ask: what decision does this answer? Expand only for the user
request, a direct import/caller/reference, an error or failed validation, or a
required security/protocol/migration/hardware contract. General curiosity is
not a reason to rediscover architecture. Reuse unchanged evidence and ranges.

## Classify before exploring
- **TARGETED** (default): known file, symbol/function, line, error, failing test,
  PR, commit, subsystem or implementation plan. Skip `docs/AI_NAVIGATION.md`,
  direct index reading and `ai_context.py`. Use exact rg → bounded read → patch
  → residual rg → lightweight validation.
- **DISCOVERY**: location/root cause unknown. Use optional
  [AI_NAVIGATION](docs/AI_NAVIGATION.md) and one route from the machine-readable
  [AI_CONTEXT_INDEX](docs/AI_CONTEXT_INDEX.json):
  `python3 scripts/ci/ai_context.py <task-id> --max-items 12`.
  A second route needs evidence that the first cannot locate the subsystem.
- **FULL_AUDIT**: only an explicit full review, production/security/architecture
  audit or broad regression investigation. Work subsystem by subsystem with
  compact conclusions; never load the whole repository at once.
- **HARDWARE_SAFETY**: PMIC, power rails, GPIO, boot, flash, storage bus,
  destructive firmware operations or physical EPD diagnosis. Read the compact
  [PhotoPainter safety contract](docs/devices/PHOTOPAINTER_SAFETY_CONTRACT.md)
  first; historical A/B evidence is on demand. Server/API, schedule, playlist,
  manifest, analysis and rendering work alone does not require full handoffs.

## Startup and reading
For implementation run `git status --short` and `git log -1 --oneline`; verify
any supplied base/HEAD. Preserve unrelated changes. No automatic broad scan.
Prefer `git ls-files` when a tracked inventory is needed, scoped `rg` for exact
references; unrestricted filesystem inventory requires a real inventory task.
For discovery start with at most 4 files / 800 lines, expanding with evidence.

Any text file over roughly **50 KB** is **symbol-first**, including source,
tests, documents and generated text: `rg -n '<symbol|term|heading>' path`, then
normally 40–120 useful lines with `sed`. Never dump the whole large file.
Reread only edited ranges, ranges implicated by an error, or adjacent context
needed for a decision. No stale hardcoded list of large files is required.

All files may be read when needed; availability is not a reason to read them.
Defer README.md, README.en.md, USER_MANUAL.html, docs/README.md, docs/archive/**,
.env*, data/session.key*, *.db, *.sqlite*, *.lock, .git/**, .ruff_cache/**,
**/__pycache__/**, data/cache/**, data/releases/**, data/backups/**, output/**,
simulation_photos/**, logs, fonts, images and firmware binaries. Start runtime
investigations from source/schema/config definitions; read DB/logs/artifacts
when those are insufficient. Local AI memory (~/.codex/memories/**) is on demand
for required prior decisions, not automatic project discovery. No path is
permanently forbidden for Token saving. Never echo secrets, commit credentials,
or put them in tests, logs, prompts or PR descriptions.

## Direct entry points (read only the relevant one)
- Prompt/Schema/Token: `inktime/app/domain/analysis/schema.py`, then
  `inktime/app/providers/openai_compatible.py` as needed.
- Ranking: `inktime/app/domain/analysis/scoring.py`.
- Vision lifecycle/cache/retry/persistence: exact symbol in
  `inktime/app/services/analysis.py` or `inktime/app/domain/analysis/plan.py`.
- Provider: `inktime/app/providers/router.py`, then active provider and directly
  relevant usage/budget service. Batch: `inktime/app/services/batch_analysis.py`
  then `inktime/app/providers/openai_batch.py`.
- Scanner/EXIF/quality: `inktime/app/workers/scanner.py`, then relevant
  `inktime/app/domain/photos/` file.
- Selection/release eligibility: `inktime/app/repositories/render_candidates.py`,
  then `inktime/app/services/display_prepare.py` if needed.
- Rendering: exact function in `inktime/app/services/rendering.py`, then relevant
  `inktime/app/domain/rendering/` file.
- Web/UI: exact template → matching API route → directly called service/repository.
- Setting: `rg -n 'exact.setting.key' inktime/app/repositories/settings.py`.
- Database: exact repository method → relevant schema/migration only; never
  rewrite a released migration or preload the complete migration history.
- CI failure: exact job/error → matching testcase → referenced source.

## Tests and PR review
Do not preload tests. **Tests are symbol-first**: use
`rg -n '<symbol|endpoint|error|behavior>' tests`, then only matching functions,
required fixtures and nearby helpers. After editing, expand only to directly
affected tests or validation/Hosted CI failures. Do not reload whole integration
suites or restart repository discovery after a failure.

**PR review is diff-first**: begin with `git diff <base>...HEAD` and
`git diff --stat <base>...HEAD`; review changed code before affected callers,
contracts and tests. A PR is not authorization to audit all of main.

## Contracts and validation
Current source defines implementation; current contracts define required
API/protocol/DB/security/deployment/hardware/recovery compatibility. Historical
reports are evidence only and never override current contracts or source.

GitHub Actions is authoritative for tests, builds, security scans, benchmarks,
firmware and hosted runtime. For ordinary coding do not run local pytest,
npm test, Docker/Compose, Playwright, Arduino/PlatformIO, benchmarks,
paid-provider calls or soak tests. Use `python3 -m py_compile <changed files>`,
`git diff --check`, and lightweight validators only when their owned files
change. Navigation changes: `python3 scripts/ci/validate_ai_navigation.py`.

After push/PR inspect new Hosted CI once. No `gh run watch`, repeated polling,
sleep loops, unsupported reruns or manual dispatches. Report
`CI_STATUS=CI_PENDING` when queued/running; a later request can check completion.

## Scope, delivery and handoff
Use an isolated branch/worktree. Keep PR Draft. No automatic merge, ready,
auto-merge, force-push, reset/clean of another worktree, or runtime/data deletion.
Fix necessary direct dependencies; report unrelated issues without expanding.

Before a substantially different task/subsystem, finish with a compact handoff:
```text
BASE_HEAD=
FINAL_HEAD=
PR=
CHANGED_FILES=
CONFIRMED_BEHAVIOR=
UNRESOLVED=
CI_STATUS=
```
Recommend a new chat for unrelated work using that handoff, rather than carrying
all tool output. Do not create a new chat automatically. Within one task reuse
known evidence. Repository policy reduces unnecessary reads; it cannot remove
client-injected history, memory, tool definitions or system instructions.

## PhotoPainter invariant
Never disable TG28 ALDO3 / Audio_VCC (`REG90[2]`): unpowered codecs can clamp
shared I2C and prevent EPD refresh. Keep ALDO3 powered, GPIO7 LOW and I2S input/
uninitialized. EPD is ALDO4. Preserve GPIO0 BOOT, GPIO5 PWR, GPIO21 IRQ and the
compact contract's narrow PMIC allowlist and recoverable flash boundary.
A build/Hosted CI pass does not establish physical panel acceptance.
