# InkTime PR #53 Final One-Shot Hardening Audit

## 基線與範圍

| 項目 | 值 |
|---|---|
| Repository | `steven87090799/InkTime` |
| PR | `#53 fix: harden final production and OpenRouter runtime` |
| Base | `origin/main 064a599bd9e13dd95c6e7d326759d8efc9b22306` |
| Start HEAD | `25db8187c4afb71651ce9bf6e6bd3cbcf25dddd8` |
| Branch | `fix/final-production-openrouter-and-runtime-hardening` |
| Worktree | `/Users/steven/Desktop/inktime/InkTime-final-production-openrouter-runtime-hardening` |
| Delivery state | Draft、Open、未 Merge、未 Mark Ready、未 Enable Auto Merge |

本輪只處理與 PR #53 直接相關的 Provider policy、ESP32 CA persistence、Migration 32 provenance、frozen repair policy、Provider Level 1/2/3 contract、benchmark quality/ranking contract、文件與 hosted CI evidence。`ConverTo6c_bmp-7/`、`data/cache/`、`data/releases/`、使用者檔案與其他工作樹未納入修改。

## 驗證政策

依施工手冊，本機禁止 pytest、ruff、mypy、pip install／pip-audit、Docker／Compose、Playwright、Arduino／PlatformIO／ESP32 編譯、runtime soak、production smoke、真實 OpenRouter API、付費呼叫與硬體操作。本機只做 Git、文字檔、差異與靜態契約核對；測試與安全掃描以 GitHub Actions hosted evidence 為準。

## 要求矩陣

| 區域 | Code／契約狀態 | Hosted／真實環境邊界 |
|---|---|---|
| OpenRouter repair privacy、routing、ZDR、usage、sticky policy | CODE PASS；共用 helper，repair 無 image／reasoning | HOSTED CONTRACT PASS 待本輪 final-head workflow；LIVE PROVIDER `NOT RUN` |
| ESP32 CA 上限與 NVS 寫入／read-back | CODE PASS；共享 `kMaxDeviceCaPemBytes=3500` 與錯誤碼契約 | HOSTED COMPILE／source contract 待 final-head；physical NVS／TLS `NOT RUN` |
| Migration 32 歷史成本來源 | CODE PASS；舊 actual cost 只可標 `estimated`／`unknown` | HOSTED migration regression 待 final-head；正式資料 upgrade `NOT RUN` |
| Frozen repair policy 與 Vision fingerprint | CODE PASS；plan freeze、最多一次文字 repair、排除 Vision identity | HOSTED Python contract 待 final-head；既有 production job runtime `NOT RUN` |
| Provider Level 1/2/3 | FAKE CONTRACT PASS；Level 2/3 明確按鈕且不自動執行 | HOSTED fake contract 待 final-head；REAL PROVIDER `NOT RUN` |
| Benchmark quality／ranking | METRIC ENGINE PASS；offline 只回 `offline-contract` | HOSTED offline contract 待 final-head；REAL MODEL QUALITY `NOT RUN` |
| 文件與 HTML 索引 | CODE／STATIC PASS；Markdown 路徑與控制項同步 | 連結／瀏覽器驗證不替代 source-aligned static review |

## Hosted provenance 規則

本報告刻意分開三種證據，不再把 Pull Request merge ref 稱為 exact-head：

- `PR_HEAD`：PR #53 當時 feature branch 的提交。
- `TESTED_MERGE_REF`：`pull_request` workflow 驗證 PR 加上當前 `main` 的合併相容性。
- `EXACT_HEAD_WORKFLOW_RUN`：最後推送後以 `workflow_dispatch` 指向 feature branch，且 `headSha == FINAL_HEAD` 的 hosted run。

既有 PR checks 是 merge-ref 證據；完成本輪 push 後，必須重新取得兩個 workflow dispatch run，逐一確認 `event=workflow_dispatch`、`headSha=FINAL_HEAD`、`conclusion=success`。最終 run ID 與 SHA 以 PR #53 Checks 及交接回報為唯一最新證據，避免在文件內留下會漂移的舊 run ID。

## 成本、隱私與硬體邊界

- Migration 32 不新增 Migration 33；歷史 `actual_cost` 沒有 provider provenance 時不得標為 `provider_reported`。
- Provider Level 2/3 與 live benchmark 只接受 deterministic synthetic／明確 non-private golden manifest；不得讀取 production photo、AI cache、release 或 display history。
- hosted CI 的 fake provider、offline benchmark、ESP32 compile 與 source contract 都不能推論真實 OpenRouter routing／ZDR／cost、NAS、正式 TLS chain、NVS、BUSY、GPIO5、deep sleep、方向、ghosting、六色顯示或功耗。
- 最終分類只能是 `CODE_READY_REAL_ENV_PENDING`；真實 OpenRouter、NAS、PhotoPainter／ESP32 與長時間現場 soak 一律 `NOT RUN`。

## 靜態交接

完成前只允許執行 `git diff --check`、`git status --short`、`git diff --stat origin/main...HEAD`、`git log --oneline origin/main..HEAD` 與指定 `rg` source checks。PR #53 必須保持 Draft、未 Merge、未 Mark Ready、未 Enable Auto Merge。
