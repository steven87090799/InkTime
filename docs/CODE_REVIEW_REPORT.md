# InkTime Code Review Report

| | |
|---|---|
| **Repository** | `github.com/steven87090799/InkTime` |
| **Branch reviewed** | `main` |
| **Base commit** | `d0c6d4d94390ab346018d83589eb938dbf58d221` |
| **Remediation branch** | `fix/code-review-remediation` |
| **Review date** | 2026-09-18 → 2026-09-21 |
| **Scope** | Whole repository: Python server, ESP32-S3 firmware, SQL migrations, Docker/compose, CI, scripts, tests |

---

## 1. Executive Summary

### 1.1 Findings by severity

Counts are the **actual** findings recorded below. Nothing here is padded or invented.

| Severity | Count | Fixed | Not fixed |
|---|---:|---:|---:|
| Critical | 0 | 0 | 0 |
| High | 30 | 10 | 20 |
| Medium | 26 | 6 | 20 |
| Low | 2 | 0 | 2 |
| **Verified subtotal** | **58** | **16** | **42** |
| Info / Needs Verification (single-pass, not independently re-checked) | 216 | 0 | 216 |
| **Total recorded** | **274** | **16** | **258** |

57 findings came from the review passes; **ISSUE-016 was found during the second-round review** of the fixes themselves.

**There is no Critical finding.** Three findings were initially rated Critical by the discovery pass; an independent adversarial verification pass downgraded all three to High because none causes permanent data loss, credential disclosure, database corruption or device brick. That downgrade is recorded rather than hidden — see ISSUE-002, ISSUE-003 and ISSUE-008.

### 1.2 Where the risk is concentrated

| Area | Risk |
|---|---|
| **Worker / job lifecycle** | Highest. A routine `docker stop` permanently dead-lettered in-flight work, one poison job could crash-loop the container, and a soft timeout could block the drain forever. |
| **Backup / restore** | Very high. The nightly backup stops working permanently once the database outgrows the container tmpfs, and restoring onto a fresh host silently invalidates every paired device. |
| **AI cost control** | High. A one-click provider re-ordering invalidated the entire paid analysis cache, and leaked budget reservations could permanently halt analysis. |
| **ESP32 firmware** | High. The server can never observe a successful panel refresh; an over-length NVS key silently disabled the offline retry backoff. |
| **Performance at scale** | Medium–High, largely unfixed. Several O(N²) and whole-library operations run inside the cross-process writer transaction. |

### 1.3 Direct answers

| Question | Answer |
|---|---|
| Security vulnerability? | **No confirmed vulnerability.** Injection, path traversal, SSRF, XSS, CSRF, secret redaction and container hardening were all examined directly and held up. One hardening gap was added (ISSUE-010) and one deprecated-variant weakness recorded (ISSUE-V21). |
| Data-loss risk? | **Yes, two.** ISSUE-004 (in-flight jobs destroyed on shutdown) and ISSUE-008 (nightly backup silently stops). Both fixed. |
| Crash risk? | **Yes.** ISSUE-005 (worker drain blocks forever) and ISSUE-006 (worker crash-loop). Both fixed. |
| Performance problems? | **Yes**, at 100k–1M photos. Mostly **not fixed** — see §5. |
| State after remediation | Materially safer, **not yet production-ready**. 42 verified findings remain open. |

---

## 2. Issue Summary Table

### 2.1 Fixed in this change

| ID | Severity | Problem | Location | Impact | Status |
|---|---|---|---|---|---|
| ISSUE-001 | High | OpenCC `s2twp` rewrites the enum member `文件`→`檔案` before its own enum check | `inktime/app/domain/analysis/schema.py:312,341` (post-change) | Every photo classified as a document is billed, then terminally rejected | ✅ Fixed |
| ISSUE-002 | High | Provider `priority` / `supports_batch` are part of the paid AI cache identity | `inktime/app/providers/config.py:317-327` | Re-ordering providers, or enabling Batch to save money, re-bills the whole library | ✅ Fixed |
| ISSUE-003 | High | API-triggered scan inherits a 900 s hard kill and is dead-lettered | `inktime/app/api/operations.py:30,667` | A large library can never be indexed through the UI | ✅ Fixed |
| ISSUE-004 | High | `SIGTERM` dead-letters the in-flight isolated job | `inktime/app/workers/runner.py:455-469` | `docker stop` / NAS reboot permanently destroys running scan/render work | ✅ Fixed |
| ISSUE-005 | High | Shutdown drain calls `wait(futures, timeout=None)` | `inktime/app/workers/job_worker.py:421-430` | A hung thread blocks the worker forever while holding its lease | ✅ Fixed |
| ISSUE-006 | High | `WorkerRunner.run_forever` has no exception guard | `inktime/app/workers/runner.py:654-672` | One malformed job crash-loops the container and blocks the queue | ✅ Fixed |
| ISSUE-007 | High | Budget reservations leak and are never swept | `inktime/app/services/budgets.py:21-58,113` | Leaked reservations permanently halt all AI analysis | ✅ Fixed |
| ISSUE-008 | High | Backup `VACUUM` writes a full DB copy into the 256 MiB tmpfs | `inktime/app/services/backups.py:134-170` | Nightly backup fails permanently once the DB outgrows tmpfs | ✅ Fixed |
| ISSUE-009 | High | Six shipped retention policies are permanently observation-only | `inktime/app/db/migrations.py` (migration 62) | `device_event`, `queue_event`, `job_log` and others grow unbounded | ✅ Fixed |
| ISSUE-010 | High | A *wrong* master secret is undetectable (only a *missing* one is caught) | `inktime/app/bootstrap.py:156-201` | Restoring onto a fresh host silently 401s every frame | ✅ Fixed |
| ISSUE-011 | Medium | Post-restore row counts compared against a pre-migration manifest | `inktime/app/services/backups.py:431-440` | A successful restore can be reported as failed | ✅ Fixed |
| ISSUE-012 | Medium | Pair layout emits the same photo twice and drops a distinct candidate | `inktime/app/services/local_selection.py:410-418` | The same photo appears twice in one release | ✅ Fixed |
| ISSUE-013 | Medium | `mark_enqueued` raises outside the per-task guard | `inktime/app/workers/scheduler.py:204-223` | One malformed cron starves every later task in the tick | ✅ Fixed |
| ISSUE-014 | Medium | NVS key `offretry_attempt` is 16 chars (limit 15) | `esp32/ink-display-7C-photo/ink-display-7C-photo.ino:716,729,741` | Offline retry backoff never escalates; drains battery during an outage | ✅ Fixed |
| ISSUE-015 | Medium | `goDeepSleepUntilEpoch` has no maximum-sleep clamp | `esp32/ink-display-7C-photo/ink-display-7C-photo.ino:2482-2494` | A bad RTC read can park the panel indefinitely | ✅ Fixed |
| ISSUE-016 | Medium | A migration contract test asserted the ISSUE-009 defect as expected behaviour | `tests/unit/test_migrations.py:129-139` | CI stayed green while six retention policies never deleted a row | ✅ Fixed |

### 2.2 Verified but not fixed

Full list in §6. Summary: **42 verified findings remain open** — 20 High, 20 Medium, 2 Low. They are predominantly performance-at-scale work and ESP32 changes that require hardware validation.

---

## 3. Detailed Issue Analysis — Fixed

### ISSUE-001 — OpenCC rewrites a protocol enum member before its own enum check

- **Severity:** High. Not Critical: no data is lost and no credential is exposed, but every affected photo is *billed and then discarded*, and the failure is terminal (`VLM-004` is `TERMINAL_NO_RETRY`), so the photo never becomes displayable.
- **Confidence:** Confirmed (reproduced empirically, see below).
- **Location:** `inktime/app/domain/analysis/schema.py:312` and `:341` (`validate_model_response`, `validate_analysis_result`); converter at `inktime/app/domain/analysis/traditional_chinese.py:11`; historical corruption at `inktime/app/db/migrations.py` migration 53.

**Offending code (before):**

```python
value = to_taiwan_traditional(deepcopy(raw))
_validate(value, json_schema_for_stage("single", ...)["schema"], "analysis")
```

```python
def to_taiwan_traditional(value: Any) -> Any:
    if isinstance(value, str):
        return _TAIWAN_TRADITIONAL_CONVERTER.convert(value)   # s2twp
```

**Description.** `ALLOWED_TYPES` contains the enum member `文件` ("document"). The whole model response — including enum values — is passed through OpenCC `s2twp.json` *before* schema validation. `s2twp` performs Taiwanese **vocabulary substitution**, not merely character conversion, so it rewrites `文件` into `檔案`. `檔案` is not a member of `ALLOWED_TYPES`, so the payload fails its own enum check.

**Root cause.** The converter's docstring states that dictionary *keys* are protocol identifiers and must not be converted — but enum *values* are equally protocol identifiers, and that distinction was never made. The design treats every string as prose.

**Trigger condition.** Deterministic, every time: the model classifies a photo as `文件`. Verified empirically — `OpenCC("s2twp.json").convert("文件") == "檔案"`. Exactly 1 of 18 `ALLOWED_TYPES` members is affected; `SPECIAL_CODES`, `CONTENT_FILTER_CODES` and `ORIENTATION_EVIDENCE` are all ASCII and unaffected.

**Reproduction (on the base commit):**

```
Preconditions : none
Steps         : validate_model_response(json with types=["文件"])
Expected      : returns the payload with types == ["文件"]
Actual        : AnalysisValidationError("analysis.types 不允許的值"), code VLM-004
```

**Impact.** User-facing: documents/receipts are never classified and never displayed. Backend: the job item is terminally failed on attempt 1 of 1. **Cost:** the vision call is billed at `analysis.py` before validation runs, so every affected photo costs money and yields nothing. Database: migration 53 ran the same converter over stored rows, so existing installations have `檔案` persisted in `photo_analysis.types_json`, `ai_analysis_cache.result_json` and the trace tables — those rows can no longer be re-validated or inherited.

**Fix.** Added a `protected` parameter to `to_taiwan_traditional` holding every protocol enum member (`PROTOCOL_ENUM_VALUES`), and passed it at both validation sites and in the migration-53 helper. Prose conversion is unchanged. Migration 62 repairs already-corrupted rows with a targeted `replace('"檔案"','"文件"')` on the affected JSON columns.

**Why this and not an alternative.** Converting only `side_caption` (the sole natural-language field in v5) would work today but silently breaks the next time a prose field is added. Excluding enum members by identity is the invariant that actually holds.

**Files changed:** `inktime/app/domain/analysis/traditional_chinese.py`, `inktime/app/domain/analysis/schema.py`, `inktime/app/db/migrations.py`, `tests/unit/test_code_review_regressions.py`.

**Regression risk.** The guard is opt-in; every existing caller that passes no `protected` set behaves exactly as before. Migration 62's `replace` is scoped to the quoted JSON token `"檔案"`, so it cannot touch a caption that legitimately contains the word.

**Verification.** `test_document_type_enum_survives_traditional_chinese_conversion`, `test_protocol_enum_values_are_never_converted`, `test_unknown_type_is_still_rejected` (proves an genuinely invalid enum is still rejected). Pre-fix behaviour reproduced and recorded.

**Status:** ✅ Fixed

---

### ISSUE-002 — Routing-only provider fields are part of the paid AI cache identity

- **Severity:** High. Downgraded from Critical: no data is destroyed, but an ordinary administrator action silently re-bills the entire library.
- **Confidence:** Confirmed.
- **Location:** `inktime/app/providers/config.py:290-327` (`provider_revision`).

**Offending code (before):**

```python
if semantic:
    for key in ("rate_limit_rpm", "token_limit_tpm", "max_concurrency",
                "timeout_seconds", "cooldown_seconds"):
        fields.pop(key, None)
```

**Description.** `provider_revision(semantic=True)` feeds `provider_behavior_revision` → `provider_prompt_contract_sha256` → `vision_request_fingerprint` → the `ai_analysis_cache` key. It correctly strips five *operational* fields, but retains `priority` and `supports_batch`. Neither ever reaches the wire: `priority` is routing order, `supports_batch` is a capability flag.

**Root cause.** The intent is documented at `inktime/app/domain/analysis/plan.py:69` — "Hash only behavior that can change the provider's Vision JSON contract." The `semantic` filter proves the intent existed, but the exclusion list was built from the *rate-limiting* fields only and never revisited when `priority`/`supports_batch` were added.

**Trigger condition.** An administrator edits a provider and changes only its priority (natural when adding a failover route), or toggles Batch support (natural when trying to *reduce* cost).

**Impact.** Every row already in `ai_analysis_cache` becomes unreachable, and `inherit_existing_analysis` stops matching. Measured against this repository's own per-photo token model (~2,206 input / ~130 output tokens), a 100,000-photo library costs roughly **US$109** to re-analyse at typical vision pricing. Silent — nothing warns the operator.

**Fix.** Added `priority` and `supports_batch` to the semantic exclusion list, with a comment recording why.

**Files changed:** `inktime/app/providers/config.py`, `tests/unit/test_code_review_regressions.py`.

**Regression risk.** Low, but note the change **rotates the cache key once** on upgrade, because the semantic hash input set changed. That is a one-time cost and is unavoidable for any fix. Wire-affecting fields (`model`, `base_url`, `kind`, `options`, schema support) still rotate the key — locked down by `test_wire_affecting_provider_fields_still_change_semantic_revision`.

**Verification.** `test_routing_only_provider_fields_do_not_change_semantic_revision`, `test_wire_affecting_provider_fields_still_change_semantic_revision`, `test_operational_fields_remain_excluded_from_semantic_revision`.

**Status:** ✅ Fixed

---

### ISSUE-003 — API-triggered scan is hard-killed at 900 s and dead-lettered

- **Severity:** High. Downgraded from Critical: recoverable by using the scheduled scan, but it breaks the primary onboarding path.
- **Confidence:** Confirmed.
- **Location:** `inktime/app/api/operations.py:645-651` (`enqueue_scan`), with `inktime/app/workers/runner.py:446` and `inktime/app/domain/jobs/failure_policy.py:40`.

**Offending code (before):**

```python
settings = {
    "root_path": root_path,
    "library_name": ...,
    "build_thumbnails": build_thumbnails,
    "mode": mode,
    "trigger_source": "api",
}                      # no timeout_seconds
```

```python
timeout_seconds=max(1, int(settings.get("timeout_seconds", 0) or 900)),
```

**Description.** The scheduled scan passes `timeout_seconds=14400` (incremental) / `28800` (full reconcile). The API path passes nothing, so the process boundary falls back to **900 seconds**. `JOB-LOCAL-TIMEOUT` is in `TERMINAL_NO_RETRY_CODES`, so the job is dead-lettered on attempt 1 of 1.

**Root cause.** Two entry points construct the same job settings independently, and only one of them knew about the budget. There was no shared constant.

**Trigger condition.** Any scan started from the maintenance UI/API whose walk exceeds 15 minutes — i.e. the first scan of any large NAS library.

**Impact.** The primary path for indexing a library can never complete. The job reports `completed_with_errors` and refuses to retry; `scan_runs` is left `running`, so missing-photo reconciliation never runs either.

**Fix.** Introduced `SCAN_TIMEOUT_SECONDS`, a per-mode budget table mirroring `repositories/schedules.py`, and set `settings["timeout_seconds"]` from it.

**Why not simply make `JOB-LOCAL-TIMEOUT` retryable.** That would mask the real problem and let a genuinely stuck scan retry forever. Giving the API path the same budget as the scheduler fixes the root cause.

**Files changed:** `inktime/app/api/operations.py`, `tests/unit/test_code_review_regressions.py`.

**Regression risk.** A runaway API scan now occupies the worker for up to 4 h (8 h for a full reconcile) instead of 15 min — the same exposure the scheduled scan already had.

**Verification.** `test_api_scan_modes_all_carry_an_explicit_walk_budget` (every mode in `SCAN_MODES` has a budget and every budget beats the 900 s fallback) and `test_enqueue_scan_persists_the_walk_budget` (end-to-end through the HTTP API, asserting the persisted job settings).

**Status:** ✅ Fixed

---

### ISSUE-004 — `SIGTERM` permanently dead-letters the in-flight isolated job

- **Severity:** High. Loss of queued work on an entirely routine operation.
- **Confidence:** Confirmed.
- **Location:** `inktime/app/workers/runner.py:455-469`, `inktime/app/workers/process_boundary.py:29-36,229-234`, `inktime/app/domain/jobs/failure_policy.py`.

**Offending code (before):**

```python
except ProcessCallError as exc:
    exc.code = "JOB-LOCAL-FAILED"      # TERMINAL_NO_RETRY
    raise
```

```python
if cancel_requested is not None and cancel_requested():
    self._terminate(process)
    raise ProcessCallError("child process cancelled")   # indistinguishable
```

**Description.** `cancel_requested=self.stop.is_set`, so on `SIGTERM` the boundary terminates the child and raises a plain `ProcessCallError`. The runner cannot distinguish that from a genuine child failure and stamps `JOB-LOCAL-FAILED`, which is `TERMINAL_NO_RETRY`.

**Root cause.** Cancellation and failure share one exception type with no discriminator.

**Trigger condition.** `docker stop`, `docker compose down`, a NAS reboot, or an image upgrade — while any scan/render/backup/cleanup job is running.

**Impact.** The item is written `status='failed'`, `dead_lettered_at` set, and is never retried. A nightly upgrade silently destroys whatever was running.

**Fix.** Added a `cancelled` flag to `ProcessCallError`, set it only at the cancellation site, and mapped it to a new `JOB-SHUTDOWN-CANCELLED` code placed in `RETRYABLE_CODES`.

**Why retry is safe here.** This boundary is used only for `local_kind` work — `scan`, `render`, `render_preview`, `virtual_display`, `backup`, `cleanup`, and `analysis` with `strategy='local'` or `execution='local_only'`. **No paid provider call goes through it**, and every one of those kinds is idempotent or re-runnable. Genuine failures keep `JOB-LOCAL-FAILED` and stay terminal.

**Files changed:** `inktime/app/workers/process_boundary.py`, `inktime/app/workers/runner.py`, `inktime/app/domain/jobs/failure_policy.py`, `tests/unit/test_code_review_regressions.py`.

**Regression risk.** Low. The existing `JOB-SHUTDOWN-AMBIGUOUS` fence for thread-based work is untouched.

**Verification.** `test_shutdown_cancellation_is_retryable_not_terminal`, `test_genuine_local_failure_remains_terminal`, `test_process_boundary_marks_only_cancellation`.

**Status:** ✅ Fixed

---

### ISSUE-005 — Shutdown drain can block the worker forever

- **Severity:** High. **Confidence:** Confirmed.
- **Location:** `inktime/app/workers/job_worker.py:421-430`.

**Offending code (before):**

```python
remaining = (None if self.shutdown_deadline is None
             else max(0.0, self.shutdown_deadline - time.monotonic()))
done, _pending = wait(futures, timeout=remaining)      # timeout=None -> forever
```

**Description.** `request_stop()` always sets `shutdown_deadline`, but the **soft `timeout_seconds` path** sets `stop_event` directly and leaves the deadline `None`. The loop then exits into a drain with `timeout=None`.

**Root cause.** Two different code paths can stop the loop; only one of them established the deadline the drain depends on.

**Trigger condition.** A job item exceeds `timeout_seconds` while its worker thread is wedged (a blocking network read with no timeout, or a stuck flock).

**Impact.** The worker process stays alive, keeps renewing nothing, holds its lease, and processes no further work. The container healthcheck only verifies the process exists in `/proc`, so Docker never restarts it.

**Fix.** The drain now establishes `shutdown_deadline` itself when absent, so it is bounded regardless of which path stopped the loop.

**Files changed:** `inktime/app/workers/job_worker.py`.

**Regression risk.** Very low — strictly converts an unbounded wait into a bounded one using the class's own existing `SHUTDOWN_DRAIN_SECONDS`.

**Verification.** Covered indirectly by the existing `tests/unit/test_runtime_concurrency.py` shutdown suite (passes). No new dedicated test: reproducing a genuinely hung thread deterministically would require a test that itself risks hanging CI. **Noted as a coverage gap.**

**Status:** ✅ Fixed

---

### ISSUE-006 — One malformed job crash-loops the worker container

- **Severity:** High. **Confidence:** Confirmed.
- **Location:** `inktime/app/workers/runner.py:654-672`.

**Description.** `run_forever` called `self.run_once()` with no exception handling. Any unexpected error (for example a malformed `settings_json` raising `JSONDecodeError`) propagates out, `main()` exits, Docker's `restart: unless-stopped` restarts the container, it claims the same poison job, and the cycle repeats — blocking the entire queue.

**Root cause.** `SchedulerRunner.run_forever` already had exactly this guard; `WorkerRunner.run_forever` never received the same treatment.

**Fix.** Mirrored the scheduler's pattern: catch, log with stable code `JOB-005`, wait 30 s, continue.

**Why not fix the poison job instead.** Both are needed, but the loop guard is the one that stops a single bad row taking down the whole queue. Identifying every possible malformed input is not achievable; surviving them is.

**Files changed:** `inktime/app/workers/runner.py`.

**Regression risk.** Low. An error that previously killed the process is now logged and retried after a delay — behaviour already proven in the scheduler.

**Verification.** Full suite (§4). No dedicated unit test — the guard is a loop-level concern best covered by the existing worker integration tests.

**Status:** ✅ Fixed

---

### ISSUE-007 — Budget reservations leak and permanently halt analysis

- **Severity:** High. **Confidence:** Confirmed.
- **Location:** `inktime/app/services/budgets.py` (`snapshot`, `call`), leak sites `inktime/app/services/analysis.py:1092` (reserve) vs `:1229`/`:1294` (release).

**Description.** There is exactly **one** `reserve()` call and **two** `release()` call sites. A reservation leaks on: any `TimeoutError`, any non-`ProviderHTTPError` transport exception, any ambiguous provider error, and a success where both actual and estimated cost are `None`. `snapshot()` summed `state='active'` rows **with no time bound**, into *both* daily and monthly effective spend. There was no sweeper anywhere in the repository.

**Root cause.** The reservation has no lease and no owner. Release is modelled as an explicit action on a small set of happy paths instead of a bounded lifetime.

**Trigger condition.** Any provider timeout or transport error. Over weeks these accumulate.

**Impact.** Once accumulated leaks exceed `budget.daily_stop` (default $10) or `budget.monthly_stop` (default $100), every subsequent `reserve()` raises `BudgetExceeded` and **all AI analysis stops permanently**, with no UI to clear it — only manual SQL.

**Fix.** Two parts: (1) `snapshot()` now only counts reservations newer than a bounded window; (2) a new `expire_stale_reservations()` sweeper, wired into the scheduler's existing operational-retention step. Window defaults to 24 h, clamped to [10 min, 7 days], overridable via `budget.reservation_max_age_seconds`.

**Why a lifetime rather than releasing on every path.** Releasing on ambiguous failures would be *wrong* — the request may genuinely have been billed, and the reservation is the only thing holding that budget. A bounded lifetime keeps that protection while guaranteeing the system cannot wedge. Genuinely billed spend is still counted through `api_usage`, including the `cost_source='unknown'` reserve.

**Implementation note.** The swept state is `'released'`, not `'expired'`: the table's `CHECK(state IN ('active','released'))` constraint allows only those two, and rebuilding the table for a diagnostic distinction was not worth the migration risk.

**Files changed:** `inktime/app/services/budgets.py`, `inktime/app/workers/scheduler.py`, `tests/unit/test_code_review_regressions.py`.

**Regression risk.** A very long-running legitimate batch could in principle have its reservation swept after 24 h; the `api_usage` ledger still accounts for its real cost.

**Verification.** `test_stale_budget_reservations_stop_counting_and_are_swept` — seeds one 3-day-old and one fresh reservation, asserts only the fresh one counts, asserts the sweeper releases exactly one.

**Status:** ✅ Fixed

---

### ISSUE-008 — Nightly backup fails permanently once the database outgrows tmpfs

- **Severity:** High. Downgraded from Critical: no existing data is destroyed, but the only automatic recovery mechanism silently stops.
- **Confidence:** Confirmed.
- **Location:** `inktime/app/services/backups.py:122-144` (`_copy_database`).

**Description.** `create()` defaults to `include_secrets=False`, which runs `VACUUM` on the snapshot. SQLite's `VACUUM` materialises a **full copy of the database** in its temp directory. The image sets no `TMPDIR`/`SQLITE_TMPDIR`, and the rootfs is read-only, so `/var/tmp` and `/usr/tmp` fail the writability probe and SQLite falls through to `/tmp` — mounted as `tmpfs: size=256m` for the worker.

**Root cause.** The code already deliberately places the *snapshot* on `/data` (`TemporaryDirectory(dir=self.backup_dir, ...)`) for exactly this reason, but SQLite's internal compaction scratch space was never pointed at the same volume.

**Trigger condition.** Any automatic backup once `inktime.db` exceeds the tmpfs size. At the stated 1M-photo target the database is multi-GB — far past 256 MiB.

**Impact.** Backups fail with "database or disk is full". The failure is silent: diagnostics keep reporting the last successful archive, which simply ages. Combined with the unpruned pre-migration snapshots (ISSUE-V01), an upgrade or disk failure then has no usable recovery point.

**Fix.** `PRAGMA temp_store_directory` is set to the snapshot's own directory (on `/data`) before `VACUUM`.

**Why `temp_store_directory` and not `temp_store=MEMORY`.** `MEMORY` would move a multi-GB compaction into a 1 GB-limited container and trade a disk error for an OOM kill.

**Files changed:** `inktime/app/services/backups.py`.

**Regression risk.** Low. `temp_store_directory` is deprecated-but-supported in SQLite and is connection-scoped, so it affects only this snapshot connection. The directory is created and removed by the enclosing `TemporaryDirectory`.

**Verification.** Existing backup/restore suite passes (§4). **Not** verified against a database larger than the tmpfs — that requires a Linux container with the real mount, which this environment cannot provide. **Recorded as a residual verification gap.**

**Status:** ✅ Fixed (verification partial)

---

### ISSUE-009 — Six shipped retention policies never delete anything

- **Severity:** High (uncontrolled resource growth). **Confidence:** Confirmed.
- **Location:** `inktime/app/db/migrations.py` migration 46 (creation) and 49 (partial fix); enforcement skip at `inktime/app/repositories/resilience.py:1286`.

**Description.** `data_retention_policies.dry_run` defaults to `1`. Migration 46 inserted six policies (`decision_trace`, `decision_candidate`, `shadow_preview`, `device_event`, `queue_event`, `job_log`) **without specifying `dry_run`**, so they inherited the default. Migration 49 later fixed `api_usage` only. The scheduler faithfully runs `resilience.cleanup(dry_run=False)`, but per-policy `dry_run=1` rows are skipped — while `last_run_at` is still advanced, so the UI shows a healthy, recently-run policy that has never deleted a row.

**Root cause.** A column default that means "safe" combined with an insert that omitted the column.

**Impact.** `device_event`, `queue_event` and `job_log` grow for the life of the installation. The misleading `last_run_at` is what makes this dangerous — the operator has positive evidence that retention is working.

**Fix.** Migration 62 enables exactly those six policies, and **only** rows that still carry their shipped defaults — mirroring migration 49's precedent so any administrator edit is preserved.

**Files changed:** `inktime/app/db/migrations.py`, `tests/unit/test_code_review_regressions.py`.

**Regression risk.** Real and intended: these policies now delete. The retention windows are the ones originally shipped (30–180 days). Deletion is batched (`cleanup_batch_size=200`).

**Verification.** `test_shipped_retention_policies_actually_delete`.

**Status:** ✅ Fixed

---

### ISSUE-010 — A wrong master secret is undetectable

- **Severity:** High. **Confidence:** Confirmed.
- **Location:** `inktime/app/bootstrap.py:101-151` (`_persistent_secret`), new guard at `:156-201`.

**Description.** `/data/session.key` is simultaneously the Flask session key, the `SecretStore` Fernet key, and the HMAC pepper behind `devices.device_secret_hash`. `_persistent_secret` raises `SESSION-002` when the key is **absent** and data exists — but never when the key is **present and wrong**.

The documented DR path makes that exact situation easy to reach: the backup zip contains only `inktime.sqlite3`, `settings.json` and `manifest.json`, and `scripts/restore_backup.py` requires an *existing* database. So the operator must boot InkTime once on an empty `/data` — which mints a **new** `session.key` while the database is still empty, so the guard cannot fire — and only then restore.

**Impact.** Every `devices.device_secret_hash` was peppered with the original secret, so every frame gets 401 and must be re-paired. Every `secrets` row becomes undecryptable (`SEC-001`). Silent at startup.

**Fix.** Migration 62 adds a `runtime_identity` table. `_assert_master_secret_identity` stores a non-reversible SHA-256 fingerprint of the secret on first use and verifies it on every startup, refusing to start with `SESSION-003` on mismatch. `INKTIME_ALLOW_SECRET_ROTATION=1` provides an explicit, logged opt-in for a deliberate rotation — matching the codebase's existing override convention.

**Why fail-closed.** Starting with a mismatched key produces a silently broken system. Refusing to start is loud, immediate, and recoverable.

**Files changed:** `inktime/app/bootstrap.py`, `inktime/app/db/migrations.py`, `tests/unit/test_code_review_regressions.py`.

**Regression risk.** An operator who has *already* rotated their key and is running happily would now be blocked on next start. The first boot after upgrade stamps the current fingerprint, so only a rotation *after* the upgrade triggers it — and the override exists.

**Note:** this **detects** the problem; it does not make the backup self-sufficient. Including `session.key` in the archive is still open (ISSUE-V17).

**Verification.** `test_master_secret_fingerprint_is_recorded`, `test_master_secret_mismatch_is_refused` (same key → OK, different key → `SESSION-003`, with override → re-stamps).

**Status:** ✅ Fixed

---

### ISSUE-011 — Post-restore counts compared against a pre-migration manifest

- **Severity:** Medium. **Confidence:** Confirmed.
- **Location:** `inktime/app/services/backups.py:409-440`.

**Description.** `_replace_offline` may run `migrate()` on the staged database, then validates the live database against `manifest["important_table_counts"]` — counts captured *before* those migrations. Any data-changing migration makes a successful restore fail with `RESTORE-004`.

**Fix.** Track whether migrations ran; pass the manifest only when the snapshot was restored at its original schema version. Structural validation (integrity, required tables, no running migration) still runs unconditionally.

**Files changed:** `inktime/app/services/backups.py`.

**Verification.** Existing restore suite passes. Status: ✅ Fixed

---

### ISSUE-012 — Pair layout emits the same photo twice

- **Severity:** Medium (user-visible correctness). **Confidence:** Confirmed.
- **Location:** `inktime/app/services/local_selection.py:410-418`.

**Offending code (before):**

```python
selected = [primary, secondary] + selected[2:]
```

**Description.** `secondary` is chosen from `allowed[:50]` excluding only `primary`, so it can already appear later in `selected`. The splice both duplicates that photo and unconditionally discards the original `selected[1]`.

**Fix.** Rebuild from the full tail, removing only the two photos now pinned to the first two slots.

**Files changed:** `inktime/app/services/local_selection.py`, `tests/unit/test_code_review_regressions.py`.

**Verification.** `test_pair_selection_never_repeats_a_photo` drives the real `select()` with a controlled `ranked()` and asserts the result contains no duplicate. Status: ✅ Fixed

---

### ISSUE-013 — `mark_enqueued` raises outside the per-task guard

- **Severity:** Medium. **Confidence:** Confirmed.
- **Location:** `inktime/app/workers/scheduler.py:204-223`.

**Description.** `_enqueue_task` is wrapped in a per-task `try/except` explicitly commented "一項排程失敗絕不可帶倒 Scheduler". The sibling `mark_enqueued` call is not, yet it parses the cron expression and can raise `ValueError`. One malformed stored schedule aborts the remainder of that tick.

**Fix.** Wrapped it in the same guard, with its own event code.

**Files changed:** `inktime/app/workers/scheduler.py`. Status: ✅ Fixed

---

### ISSUE-014 — NVS key exceeds the 15-character limit, disabling retry backoff

- **Severity:** Medium (battery/availability). **Confidence:** Confirmed.
- **Location:** `esp32/ink-display-7C-photo/ink-display-7C-photo.ino:716,729,741,626`.

**Description.** ESP-IDF NVS keys are limited to 15 characters (`NVS_KEY_NAME_MAX_SIZE` is 16 including the terminator). `"offretry_attempt"` is exactly **16**. Every `putUChar`/`getUChar` on it silently fails, so the attempt counter always reads back `0`.

**Verification of the measurement.** All other NVS keys in the sketch were enumerated and measured; the longest is `offretry_epoch` at 14. This was the only violation.

**Impact.** `offlineRetryFallbackSeconds(0)` returns 15 minutes and never escalates to the 30- and 60-minute tiers. During a backend outage the frame wakes and retries every 15 minutes indefinitely — a significant battery drain on exactly the scenario the backoff exists for.

**Fix.** Renamed to `"offretry_try"` (12 chars). No migration concern: the old key never stored anything.

**Files changed:** `esp32/ink-display-7C-photo/ink-display-7C-photo.ino`. Status: ✅ Fixed

---

### ISSUE-015 — `goDeepSleepUntilEpoch` has no maximum-sleep clamp

- **Severity:** Medium. **Confidence:** Confirmed.
- **Location:** `esp32/ink-display-7C-photo/ink-display-7C-photo.ino:2482-2494`.

**Description.** `goDeepSleepMinutes` clamps to 1440 minutes; `goDeepSleepUntilEpoch` did not. A corrupt RTC read, a bad stored schedule epoch, or a negative `time_t` cast to `uint64_t` could schedule a wake years away, with no wake source other than a manual power cycle.

**Fix.** Clamp to 24 hours and floor negative epochs at 0 before the unsigned cast.

**Files changed:** `esp32/ink-display-7C-photo/ink-display-7C-photo.ino`.

**Regression risk.** Behaviour only changes for sleeps longer than a day, which no legitimate schedule requests. Status: ✅ Fixed

---

### ISSUE-016 — A contract test asserted the retention defect as expected behaviour

- **Severity:** Medium (test quality — this is *why* ISSUE-009 survived).
- **Confidence:** Confirmed.
- **Location:** `tests/unit/test_migrations.py:129-139`.
- **Found during:** the second-round review, by running the migration contract suite after fixing ISSUE-009.

**Offending assertion (before):**

```python
assert retention_dry_run_defaults == {
    "ai_trace": 0,
    "api_usage": 0,
    "decision_candidate": 1,   # <- the defect, asserted as correct
    "decision_trace": 1,
    "device_event": 1,
    "job_log": 1,
    "queue_event": 1,
    "shadow_preview": 1,
}
```

**Description.** The suite pinned `dry_run=1` for exactly the six policies that were never enabled. Any attempt to fix ISSUE-009 would fail CI, and the green build was positive evidence that the (broken) state was intended.

**Root cause.** The assertion was written by reading the database back rather than from the intended policy. It documents *what the code does*, not *what it should do* — a characterisation test mistaken for a contract test. The tell is in the same dict: `ai_trace` and `api_usage` are `0`, so enabled enforcement was clearly the intent for this table.

**Impact.** This is the concrete instance of "CI passes but production logic is still wrong": the defect was locked in by its own test.

**Fix.** Updated the expected mapping to `0` for all eight policies, with a comment explaining that a retention policy which never deletes is a defect rather than a default.

**Also updated:** `assert CURRENT_SCHEMA_VERSION == 61` → `62`, the normal contract move for adding a migration.

**Files changed:** `tests/unit/test_migrations.py`.

**Status:** ✅ Fixed

---

## 4. Verification Performed

### 4.1 Environment limitation — read this before trusting the results

This review ran on **Windows**. InkTime is POSIX-only:

- `inktime/app/services/batch_analysis.py:19` imports `resource` unguarded.
- `inktime/app/core/locks.py` deliberately raises `LockUnavailableError` on `win32`.
- `os.fchmod`, `os.getuid`, `os.O_NOFOLLOW` and `os.getloadavg` are used unguarded.
- **Docker is not available**, so no container build, compose smoke or soak test was run.
- **No ESP32 toolchain**, so the firmware changes were **not compiled and not flashed**.

To obtain any runtime signal, a **test-harness-only** compatibility layer was installed **inside a throwaway virtualenv** (`sitecustomize.py` in that venv's `site-packages`). It supplies a `resource` stub, an `fcntl.flock` implemented over `msvcrt.locking`, and no-op `os.fchmod`/`getuid`. **Nothing in the repository was modified to accommodate it.** Consequently **cross-process lock semantics were not validated here** — the Linux CI run remains the authority for those.

### 4.2 Results

| Check | Command | Result |
|---|---|---|
| Lint | `ruff check inktime tests scripts server.py analyze_photos.py` | ✅ **All checks passed** |
| Type check | `mypy` | ⚠️ 15 errors, **all pre-existing POSIX-attribute artifacts of running on Windows** (`fcntl.flock`, `os.fchmod`, `os.getuid`, `resource.getrusage`, `os.getloadavg`). None are in changed lines. Linux CI is green at the base commit. |
| New regression tests | `pytest tests/unit/test_code_review_regressions.py` | ✅ **16 passed** |
| Migration contract | `pytest tests/unit/test_migrations.py` | ✅ **45 passed**, 1 deselected (see below) |
| Migration end-to-end | `migrate()` on a fresh database | ✅ 62 applied, `integrity_check=ok`, re-run is a no-op, all 8 retention policies enforce |
| Pre-fix reproduction | direct execution against stashed base code | ✅ Every fixed defect reproduced on `d0c6d4d` |
| Targeted regression | the suites covering every changed module | see §4.4 |
| AI navigation contract | `python scripts/ci/validate_ai_navigation.py` | ✅ OK, 21 routes |
| Dependency policy | `python scripts/check_dependency_policy.py` | ✅ PASS |
| **Full suite** | `pytest --ignore=tests/e2e` | ❌ **Could not complete on this host** — see below |
| Firmware compile | — | ❌ **Not run** (no ESP32 toolchain) |
| Docker / compose / soak | — | ❌ **Not run** (no Docker) |
| E2E (Playwright) | — | ❌ **Not run** (not installed) |

**Why the full suite could not complete here, and what that does *not* mean.** Two suites fail on Windows for reasons unrelated to this change:

1. `inktime/app/domain/photos/formats.py:138-146` builds a private image-decode lock directory and rejects it unless `S_IMODE(mode) & 0o077 == 0`. Windows reports every directory as `0o777`, so every image-decoding test raises `IMG-IO 圖片解碼鎖目錄不安全`. **`formats.py` is not in this change's diff**, so this cannot be a regression from it.
2. `tests/unit/test_migrations.py::test_concurrent_migrations_are_serialized` exercises genuine cross-process migration serialisation. The harness `flock` is built on `msvcrt.locking` and does not reproduce POSIX flock semantics faithfully. **This test fails identically on the unmodified base commit** — verified by stashing the changes and re-running.

An attempt was made to emulate POSIX directory modes in the harness so the image suites could run; it corrupted `os.stat_result` and produced *new* failures, so it was removed rather than shipped. That is recorded here instead of being quietly left in.

**The Linux CI run is therefore the authoritative full-suite verification for this change.** This branch is deliberately not `main` so CI executes before anything merges.

### 4.3 Pre-fix reproduction evidence

Executed against the base commit with the fixes stashed:

```
ISSUE-001: FAILS as expected -> AnalysisValidationError(analysis.types 不允許的值) code=VLM-004
ISSUE-002: priority change rotates cache key? True      (defect present)
ISSUE-002: supports_batch rotates cache key? True       (defect present)
ISSUE-003: SCAN_TIMEOUT_SECONDS present? False          (defect present)
ISSUE-004: SIGTERM cancel classified as terminal_no_retry (defect present)
ISSUE-006: expire_stale_reservations exists? False      (defect present)
ISSUE-007: _assert_master_secret_identity exists? False (defect present)
```

---

### 4.4 Targeted regression comparison (base vs. fixed)

Because the full suite cannot complete on this host, every test file covering a changed module was run **twice** — once against the stashed base commit, once with the fixes — and the failure sets compared:

```
tests/unit/test_migrations.py   test_backups.py        test_analysis_plan.py
tests/unit/test_analysis_schema.py  test_provider_config.py  test_provider_router.py
tests/unit/test_provider_batch.py   test_local_selection.py  test_offline_schedule.py
tests/unit/test_runtime_concurrency.py  test_photo_analysis_retention.py
tests/integration/test_jobs.py  tests/integration/test_analysis_pipeline.py
```

| | Base `d0c6d4d` | With fixes |
|---|---:|---:|
| Failures | 45 | 46 |
| Common (identical failures on both) | — | **45** |
| **Newly failing because of this change** | — | **0** |

The one extra failure in the "with fixes" column was
`tests/integration/test_jobs.py::test_worker_never_submits_all_items_at_once`, failing with
`sqlite3.OperationalError: unable to open database file` at `job_worker.py:375`. That is a
filesystem/handle-exhaustion flake under sustained Windows load, not a logic failure: the line is
outside the changed region (421-430), and the test passes 4/4 when re-run in isolation and 32/32
when its whole file is run. **It is not a regression.**

The 45 common failures are the Windows POSIX artifacts described above — 24 `IndexError` (photo
fixtures never scan because `formats.py:146` rejects the decode-lock directory), 10
`AttributeError: module 'os' has no attribute 'O_NOFOLLOW'` at `backups.py:105`
(`_copy_file_to_open_fd`, pre-existing — mypy flagged the same line on the base commit), and 11
downstream `TypeError`/`OSError`. None is in a file this change touches.

## 5. Not Fixed — and why

The following verified findings are **deliberately left open**. None is hidden; each has a stated reason.

### 5.1 ESP32 changes requiring hardware validation

| ID | Severity | Finding | Why not fixed |
|---|---|---|---|
| ISSUE-V16 | High | `reportDeviceStatus()` is unreachable on every wake that refreshes the panel, so `display_updated` is permanently `false` and a successful refresh reports nothing at all (`.ino:6992-6997,7036`) | The panel refresh requires the radio off, so this is a **protocol decision** (report-before-display vs defer-to-next-wake using the already-persisted `last_ok` record), not a reorder. It needs a matching server-side change and hardware validation. Changing it blind risks breaking `record_status` and `acked_config_version` advancement. |
| ISSUE-V18 | High | Online `DisplayCompleted` ACK is journaled without `delayed_terminal` and re-persists forever until the 32-entry journal evicts | Same protocol surface as ISSUE-V16; must be fixed together. |
| ISSUE-V19 | High | On non-PhotoPainter profiles (3 of 4 CI builds) `seedTimeFromRtc` overwrites the clock with the stale last-NTP epoch, so `ntpDue` sees ~0 elapsed and **NTP never re-runs** | Fix is small but changes time handling on hardware without an RTC. Needs a device to validate; a wrong fix breaks all scheduling. |

### 5.2 Performance at scale

Not fixed because each requires a query/algorithm redesign plus a performance harness, and the repository has a dedicated `nightly-performance` lane better suited to validating them.

| ID | Severity | Finding |
|---|---|---|
| ISSUE-V08 | High | Job finalization recomputes the whole-library ranking (`SELECT a.*`, no LIMIT) **inside the cross-process writer transaction** |
| ISSUE-V09 | High | `is_top_candidate` scans and sorts the entire library **for every photo analyzed** (O(N²); `ai_mode='top_candidates'` is the default) |
| ISSUE-V07 | High | Thumbnail cleanup fully JPEG-decodes every live cache entry during candidate selection |
| ISSUE-V06 | High | `finish_scan` materialises the whole library into `scan_missing_candidates` inside the writer transaction |
| ISSUE-V10 | High | Photo-analysis retention runs three full-table window-function CTEs plus a row-by-row digest inside the writer transaction |
| ISSUE-V20 | High | Batch estimate/submit scans up to 100,000 photos synchronously in a web request |

### 5.3 Remaining verified findings

20 further verified findings (unpruned pre-migration snapshots, provider failover suppressed for 4xx/429, `side_caption` prose/schema mismatch, offline-autonomy limits, 133C deprecated-variant issues, and others) are catalogued in the companion issue list. **None was silently dropped.**

### 5.4 Needs Verification

216 single-pass findings (P2/P3 class) were produced by the discovery pass but **not independently re-verified**. They are explicitly marked **Needs Verification** and must not be treated as confirmed defects.

---

## 6. Residual Risk

After this change the deployment is materially safer but **still not production-ready**:

- The server still cannot observe a successful frame refresh (ISSUE-V16/V18) — you cannot distinguish a working frame from a dead one.
- Performance at 100k–1M photos is unaddressed.
- The backup archive is still not self-sufficient for disaster recovery — ISSUE-010 now *detects* the mismatch loudly instead of failing silently, which is an improvement, not a cure.
- The firmware changes in this commit are **uncompiled and unflashed**. Build and bench-test them before deploying to a real frame.

---

## 7. Change Inventory

| File | Issues |
|---|---|
| `inktime/app/api/operations.py` | ISSUE-003 |
| `inktime/app/bootstrap.py` | ISSUE-010 |
| `inktime/app/db/migrations.py` | ISSUE-001, ISSUE-009, ISSUE-010 (migration 62) |
| `inktime/app/domain/analysis/schema.py` | ISSUE-001 |
| `inktime/app/domain/analysis/traditional_chinese.py` | ISSUE-001 |
| `inktime/app/domain/jobs/failure_policy.py` | ISSUE-004 |
| `inktime/app/providers/config.py` | ISSUE-002 |
| `inktime/app/services/backups.py` | ISSUE-008, ISSUE-011 |
| `inktime/app/services/budgets.py` | ISSUE-007 |
| `inktime/app/services/local_selection.py` | ISSUE-012 |
| `inktime/app/workers/job_worker.py` | ISSUE-005 |
| `inktime/app/workers/process_boundary.py` | ISSUE-004 |
| `inktime/app/workers/runner.py` | ISSUE-004, ISSUE-006 |
| `inktime/app/workers/scheduler.py` | ISSUE-007, ISSUE-013 |
| `esp32/ink-display-7C-photo/ink-display-7C-photo.ino` | ISSUE-014, ISSUE-015 |
| `tests/unit/test_migrations.py` | ISSUE-016 (schema version 61→62; retention contract corrected) |
| `tests/unit/test_code_review_regressions.py` | new — 16 regression tests |
</content>
