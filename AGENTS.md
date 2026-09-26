# InkTime agent rules

## Smallest useful context
Finish the task with only evidence needed for correctness. Reuse unchanged
facts; do not rediscover the repository each turn. Work from the Git root, not
its parent, sibling worktrees, deployments or runtime directories. Start with
`git status --short` and `git log -1 --oneline`; preserve unrelated changes.

- **TARGETED** (default): known file, symbol, error, test, PR or subsystem.
  Skip `docs/AI_NAVIGATION.md`, direct index reading and `ai_context.py`.
  Exact `rg` → bounded read → edit → residual search → relevant validation.
- **DISCOVERY**: unknown location. Use one route from optional
  [AI_NAVIGATION](docs/AI_NAVIGATION.md) via
  `python3 scripts/ci/ai_context.py <task-id>`; the
  [AI_CONTEXT_INDEX](docs/AI_CONTEXT_INDEX.json) is machine data, not a reading list.
- **FULL_AUDIT**: only when explicitly requested; inspect one subsystem at a time.
- **HARDWARE_SAFETY**: PMIC/power/GPIO/boot/flash/storage-bus/physical EPD work
  first reads [PhotoPainter safety](docs/devices/PHOTOPAINTER_SAFETY_CONTRACT.md).
  Expand to exact guide/historical A/B sections when needed, not for server-only work.

## Reading and output limits
First pass: at most **3 files / 300 lines** beyond these rules; these are ceilings,
not quotas. Usually read 40–100 lines around a match. Expand for a direct caller,
contract, failure or safety boundary; state the reason briefly. Never omit a
necessary dependency or safety check to meet the budget.

Any text over **50 KB** is **symbol-first**. **Tests are symbol-first**: locate the
behavior, then only matching cases/fixtures. **PR review is diff-first**:
`git diff --stat <base>...HEAD` then `git diff <base>...HEAD -- <relevant paths>`.
Do not print entire suites, logs, JSON, generated files or truncated search dumps;
narrow the query instead. Prefer scoped `rg -l` / `git ls-files` before content.
Keep tool output normally within 120 lines; report conclusions, not repeated logs.

Defer README.md, README.en.md, USER_MANUAL.html, docs/README.md, docs/archive/**,
dated audits/reports, .env*, session.key*, *.db, *.sqlite*, *.lock, .git/**,
data/**, output/**, photos/**, simulation_photos/**, logs/**, caches, dependencies,
fonts, images, PDFs, ZIPs and firmware binaries. Read only an exact needed path
or excerpt after source/config/schema evidence is insufficient; never echo secrets.
These are reading rules, not filesystem access controls; `.gitignore` is not one.
Do not preload AI memory or other conversations for general project discovery.
No automatic subagents, broad web search or full test logs for a narrow local task.

## Source and compatibility
Route/UI → service → repository/DB; domain owns pure rules; providers own external
calls; workers own background execution. Start at the named layer. Settings:
search exact key in `inktime/app/repositories/settings.py`. Version/default queries:
[CURRENT_STATE](docs/reference/CURRENT_STATE_ZH_TW.md), then its cited source only.
Historical reports are dated evidence, not current contracts. Keep v4/v5 stored
analysis readable, fixed ranking and one-image normal analysis; never auto-resend
an ambiguous paid request. Released migrations are append-only. Preserve data.

## Validation and delivery
Hosted CI is authoritative for tests/builds/security/benchmarks/runtime/firmware.
No routine local pytest, npm test, Docker/Compose, Playwright, firmware compile,
paid-provider calls or soak. Locally use `git diff --check`, Python syntax and
lightweight validators for changed ownership only. Docs/navigation changes:
`python3 scripts/ci/validate_ai_navigation.py` (also checks current doc contracts).
After push/PR inspect CI once; queued/running → `CI_STATUS=CI_PENDING`. No watch,
poll/sleep loops, unsupported reruns or manual dispatch to manufacture green.
Use an isolated branch/worktree; keep PR Draft. No automatic Ready, merge,
auto-merge, force-push, reset/clean of others' work, or runtime/data deletion.

## Handoff and hardware invariant
For unrelated work suggest a fresh task with a short handoff, without creating it:
`BASE_HEAD= FINAL_HEAD= PR= CHANGED_FILES= CONFIRMED_BEHAVIOR= UNRESOLVED= CI_STATUS=`.
Within the same task retain relevant evidence. These rules cannot remove
client-injected history, memories, tools or system instructions or guarantee quota.
Never disable TG28 ALDO3 / Audio_VCC (`REG90[2]`): unpowered codecs can clamp I2C.
Keep ALDO3 powered, GPIO7 LOW and I2S input/uninitialized; EPD is ALDO4. Preserve
GPIO0 BOOT, GPIO5 PWR, GPIO21 IRQ, the narrow PMIC allowlist and recoverable flash.
Hosted CI does not establish physical panel acceptance.
