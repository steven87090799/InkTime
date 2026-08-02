# InkTime 分支整併與驗證報告

## 基準與範圍

- 本次 `main` worktree：`/Users/steven/Desktop/inktime/InkTime-caption-controls`
- 回滾基準：`pre-branch-consolidation-20260801T195556Z`，建立於原始 `main` `dc6614cc2f1969056de52ce96e20ece64296b2cf`。
- 本次主線快照（最終證據文件提交前）：`4bab2a431af86cc06861e4f6c955c43db8f1288f`。
- 最終程式與文件證據提交：`4eb8b0d93f9078ea5d67efee3b975f4215a2e4e3`；main hosted run `30720945614` 的 8/8 jobs 全數成功。
- 第一個整併後 rollback tag：`post-branch-consolidation-20260801T224520Z`，指向 `4eb8b0d93f9078ea5d67efee3b975f4215a2e4e3`。
- GitHub branch protection 與 rulesets 查詢結果：未設定；因此本報告以實際 hosted checks、merge commit 與 ancestry 驗證為準。
- 所有原有工作樹與未追蹤使用者檔案均保留；沒有使用 reset、clean、force push 或覆寫其他工作樹。

## 既定合併鏈

| PR | head | merge commit | hosted 證據 | 結果 |
|---|---|---|---|---|
| [#32](https://github.com/steven87090799/InkTime/pull/32) | `aa7f494de7e8f2f0a43765b08cd9aecf3c5eb669` | `a3947dd4aa1a5aba6526ca50db39b73ba554ef3d` | 原 PR checks 全數成功 | 已合併到 `main` |
| [#33](https://github.com/steven87090799/InkTime/pull/33) | `1c967246dbb6b37211f38b4b4f71f03e743ea86b` | `faa0d66bcebf3118ccdadaa872896802c53cf27e` | 新 base merge commit 的 16/16 checks 成功 | 已合併到 `main` |
| [#34](https://github.com/steven87090799/InkTime/pull/34) | `c86ae1b39902a2c3cb0b16ad476758626df56933` | `a5e02bd1abd29dc1176c0516b54a8a9da4259502` | 新 base merge commit 的 16/16 checks 成功 | 已合併到 `main` |
| [#41](https://github.com/steven87090799/InkTime/pull/41) | `f6dc3f365dc8b28f15690f9f18a33cc5b4d34c12` | `4bab2a431af86cc06861e4f6c955c43db8f1288f` | 文件整合的 16/16 checks 成功 | 已合併到 `main` |

每一個已合併 head 都用 `git merge-base --is-ancestor <head> origin/main` 驗證為 `main` ancestry。

## PR #34 與 Migration 26

- Migration 1–25 未修改；以 base `origin/main` 對 Batch 整合 head 的 migration diff 檢查結果只有新增 Migration 26。
- Migration 26 新增 Batch input/output/error File、privacy flags、provider identity、pricing/token 欄位、batch/item 狀態、lease、cleanup 與唯一性索引。
- `tests/unit/test_migrations.py`、Batch lifecycle、payload memory、provider batch focused tests 通過；完整本地 pytest 也通過。
- Batch cleanup 對既有 terminal semantic 做保護：`completed`、`completed_with_errors`、`failed`、`cancelled`、`expired` 不會因 cleanup retry 被改寫；並加入 scheduler claim 與 cleanup worker concurrency regression tests。

## GitHub Actions 警告修正

- `actions/upload-artifact` 固定到官方 `v7.0.1` Node 24 SHA `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`。
- Arduino action 沒有可用的官方 Node 24 release，因此改用官方 Arduino CLI `1.5.1` release tarball，並驗證 SHA-256 `28a8e119c498a25607821c36cb2dc49e8463941b261a0d99091baa7bc692dd2b`；保留 ESP32 compile gate。
- PR #34 新 head 的兩組 hosted logs 沒有 Node 20／Node 20 runner deprecation warning。仍可見的 `punycode` 與 `url.parse` 訊息來自 gitleaks action 內部 Node 套件，不是 Node 20 action runtime 警告。

## 遠端分支完整盤點

以下是 final evidence commit 前 `origin` 的全部非 `main` remote refs。`eligible-delete` 只表示已經證明合併、等 final main CI 與 rollback tag 完成後才可刪除；本報告建立時尚未刪除。

| remote branch | head | 關聯 PR | 判定 |
|---|---:|---|---|
| `dependabot/docker/python-3.14-slim` | `7cf5fa5` | #35 open | `retain-open`；未審查的 dependency PR |
| `dependabot/pip/fonttools-4.63.0` | `74efd5a` | #36 open | `retain-open`；未審查的 dependency PR |
| `dependabot/pip/opencv-python-headless-5.0.0.93` | `cfb6a6b` | #40 open | `retain-open`；涉及 major dependency 變更 |
| `dependabot/pip/pillow-heif-1.5.0` | `bd69b4d` | #38 open | `retain-open`；未審查的 dependency PR |
| `dependabot/pip/pip-audit-2.10.1` | `3df4b35` | #39 open | `retain-open`；未審查的 dependency PR |
| `dependabot/pip/pytest-9.1.1` | `1e6f934` | #37 open | `retain-open`；未審查的 dependency PR |
| `docs/architecture-scoring-guide` | `8472ee5` | #2/#3 merged | `eligible-delete`；已由歷史 merge commit 覆蓋 |
| `feat/local-only-selection-and-dual-caption-layouts` | `94074af` | #31 merged | `eligible-delete`；唯一後續 docs commit 已由 #41 整合 |
| `feat/openai-batch-analysis-lifecycle` | `c86ae1b` | #34 merged | `eligible-delete`；已進 `main` |
| `feat/production-device-resilience-hardening` | `a2a21fe` | #30 merged | `eligible-delete`；已進 `main`，舊 head superseded |
| `feat/visual-orientation-correction` | `278d34a` | #26 merged | `eligible-delete`；已進 `main` |
| `feature/adaptive-frame-layout` | `1690523` | #22 merged | `eligible-delete`；已進 `main` |
| `feature/caption-controls` | `7a7e749` | #23 merged | `eligible-delete`；已進 `main` |
| `feature/console-ia-scoring-display` | `598ee58` | #11 merged | `eligible-delete`；已進 `main` |
| `feature/control-center-governance` | `7d9317a` | #25 merged | `eligible-delete`；已進 `main` |
| `feature/docker-low-resource-esp32` | `ea712fa` | #5 merged | `eligible-delete`；已進 `main` |
| `feature/e6-renderer-device-test` | `d8f94f9` | #16 merged | `eligible-delete`；已進 `main` |
| `feature/final-review-and-hardening` | `d15637d` | #19 merged | `eligible-delete`；已進 `main` |
| `feature/incremental-photo-scan` | `772cd9a` | #8 merged | `eligible-delete`；已進 `main` |
| `feature/inktime-platform-hardening` | `d8abef7` | #1 merged | `eligible-delete`；已進 `main` |
| `feature/local-photo-prefilter-location` | `ddd3cd4` | #10 merged | `eligible-delete`；已進 `main` |
| `feature/low-resource-scheduler` | `8bc02fd` | #13/#17 merged | `eligible-delete`；已進 `main` |
| `feature/photo-quality-and-ai` | `27b9da2` | #14/#18 merged | `eligible-delete`；已進 `main` |
| `feature/sqlite-safe-scanner` | `6fb2bda` | #12/#15 merged | `eligible-delete`；已進 `main` |
| `feature/traditional-chinese-font-library` | `0e704ab` | #7 merged | `eligible-delete`；已進 `main` |
| `feature/virtual-epaper-receiver` | `17d5aa5` | #9 merged | `eligible-delete`；已進 `main` |
| `feature/waveshare-photopainter` | `29f8d9d` | #6 merged | `eligible-delete`；已進 `main` |
| `fix/final-cross-module-device-hardening` | `f9f0972` | #21 merged | `eligible-delete`；已進 `main` |
| `fix/lan-production-finalization` | `1c96724` | #33 merged | `eligible-delete`；已進 `main` |
| `fix/legacy-memory-data-safety` | `cc3b4ad` | #27 merged | `eligible-delete`；已進 `main` |
| `fix/python-quality-baseline` | `727554b` | #20 merged | `eligible-delete`；已進 `main` |
| `fix/security-production-hardening` | `aa7f494` | #32 merged | `eligible-delete`；已進 `main` |
| `integration/docs-manual-consolidation` | `f6dc3f3` | #41 merged | `eligible-delete`；已進 `main` |
| `perf/runtime-concurrency-hardening` | `ea3aa3e` | #28 merged | `eligible-delete`；已進 `main` |
| `refactor/legacy-boundary-retirement` | `57b9417` | #29 merged | `eligible-delete`；已進 `main` |

已刪除的歷史 PR head（例如 #4、#24 對應的 branch）不在本次 current remote inventory，因此未被重新刪除。所有 `retain-open` dependency branches 均保留。

## 清理後複核

- 在 final hosted run 成功與 rollback tag 建立後，已依上表明列刪除 29 個 `eligible-delete` remote branches；沒有使用 wildcard，也沒有刪除 `main` 或 Dependabot branches。
- 清理後 `origin` 僅保留 `main` 與六個 open Dependabot branches：#35、#36、#37、#38、#39、#40。
- 所有本地 branches 與原有 worktrees 仍保留，包含仍被本地 worktree 使用的歷史分支；本次只清理 remote refs。

## 驗證邊界

### 已執行並可重現

- local：完整 pytest、Batch focused pytest、migration tests、ruff check、ruff format check、YAML parse、`git diff --check`、manual/docs link scan。
- hosted：#32 原有 checks；#33、#34、#41 各 16/16 checks；包含 Python 3.10/3.12、Compose persistence/TLS、runtime soak、Playwright、secret scan 與 ESP32 compile。
- software only：CI compile 與 simulator/mock 不等於實體硬體驗收。

### 明確 NOT RUN

- 真實 OpenAI API / Batch：沒有使用 production API key；只驗證 fake/provider contract、payload、cleanup 與 hosted CI。1–3 張非敏感照片的 live smoke 必須由授權人員另行執行。
- 真實 NAS：未連接 production NAS 或真實 volume；Compose persistence 是軟體環境證據，不是 NAS 實機證據。
- 真實 ESP32 / PhotoPainter：未接板、PMIC、面板或電流量測設備；ESP32 compile 成功不代表 BUSY、方向、六色、殘影、GPIO5 或 deep sleep 通過。

詳細手順與 gate 見 [`post-merge-hardware-validation.md`](post-merge-hardware-validation.md)。

## Dependency alert

GitHub Dependabot API 目前回報 `pytest` alert #1 為 `fixed`，`fixed_at=2026-08-01T21:12:48Z`；其 advisory 的 first patched version 是 `9.0.3`，而目前 `main:requirements-dev.txt` 已是 `pytest==9.0.3`。`dismissed_at` 為 null，沒有做假性 dismiss；#37 的 `9.1.1` upgrade PR 保留給獨立 dependency review。
