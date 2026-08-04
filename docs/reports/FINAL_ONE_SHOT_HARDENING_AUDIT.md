# InkTime PR #53 Final One-Shot Hardening Audit

## 基線與範圍

| 項目 | 值 |
|---|---|
| Repository | `steven87090799/InkTime` |
| PR | `#53 fix: harden final production and OpenRouter runtime` |
| Base | `origin/main 064a599bd9e13dd95c6e7d326759d8efc9b22306` |
| Start HEAD | `de9389f0e52edb96115f3b2499a05fbeaf8d4854` |
| Branch | `fix/final-production-openrouter-and-runtime-hardening` |
| Worktree | `/Users/steven/Desktop/inktime/InkTime-final-production-openrouter-runtime-hardening` |
| Delivery state | Draft、Open、未 Merge、未 Mark Ready、未 Enable Auto Merge |

本輪只處理與 PR #53 直接相關的 P1-001 ESP32 crash-consistent Config persistence、P2-001~004 benchmark coverage／cost／manifest／repair accounting、private HTTP destination safety、Provider diagnostics、Level 3 reasoning、max-cost 文件與 hosted CI evidence。Migration 32 provenance、ESP32 CA limit、scanner、scheduler、release 與 PhotoPainter rendering architecture 不在本輪重構。`ConverTo6c_bmp-7/`、`data/cache/`、`data/releases/`、使用者檔案與其他工作樹未納入修改。

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
| P2-01 remote config persistence | CODE PASS；candidate 先 NVS write/read-back，成功後才 commit runtime cfg | HOSTED ESP32 source contract／compile 待 final-head；實體 NVS `NOT RUN` |
| P2-02 Top-K tie bound | CODE PASS；deterministic exact-K，不超過 100% | HOSTED benchmark contract 待 final-head |
| P2-03 production ranking reuse | CODE PASS；直接重用 `calculate_ranking_score()` 與 `ranking-v2` | HOSTED benchmark contract 待 final-head；REAL MODEL QUALITY `NOT RUN` |
| P2-04 golden manifest | CODE PASS；canonical exclusion、strict fail-closed、network 前過濾 | HOSTED benchmark contract 待 final-head；REAL DATASET `NOT RUN` |
| Conditional P2 private HTTP | CODE PASS；generic HTTP 僅 literal private/loopback IP 且明確 opt-in，redirect disabled | HOSTED provider/security tests 待 final-head；REAL network `NOT RUN` |
| Provider diagnostics／reasoning／max-cost | CODE PASS；privacy snapshot、Level 3 不硬 forcing low、bounded post-response wording | HOSTED contract／docs checks 待 final-head；REAL PROVIDER `NOT RUN` |
| 文件與 HTML 索引 | CODE／STATIC PASS；Markdown 路徑與控制項同步 | 連結／瀏覽器驗證不替代 source-aligned static review |

## Hosted provenance 規則

本報告刻意分開三種證據，不再把 Pull Request merge ref 稱為 exact-head：

- `PR_HEAD`：PR #53 當時 feature branch 的提交。
- `TESTED_MERGE_REF`：`pull_request` workflow 驗證 PR 加上當前 `main` 的合併相容性。
- `EXACT_HEAD_WORKFLOW_RUN`：最後推送後由 PR workflow 驗證 feature branch，且 `headSha == FINAL_HEAD` 的 hosted run；本倉庫目前不以 merge-ref 或舊 run 代替這項證據。

既有 PR checks 是 merge-ref 證據；完成每次 push 後，必須重新取得兩個 exact-head run，逐一確認 `headSha=FINAL_HEAD`、`conclusion=success`。最終 run ID 與 SHA 以 PR #53 Checks 及交接回報為唯一最新證據，避免在文件內留下會漂移的舊 run ID。

## 成本、隱私與硬體邊界

- Migration 32 不新增 Migration 33；歷史 `actual_cost` 沒有 provider provenance 時不得標為 `provider_reported`。
- Provider Level 2/3 與 live benchmark 只接受 deterministic synthetic／明確 non-private golden manifest；不得讀取 production photo、AI cache、release 或 display history。
- Container evidence 的 Trivy 語意是 `unallowlisted policy findings = PASS`；temporary unfixed CVE allowlist 仍存在，expiry 為 `2026-09-30`，不可寫成 `0 vulnerabilities`。
- hosted CI 的 fake provider、offline benchmark、ESP32 compile 與 source contract 都不能推論真實 OpenRouter routing／ZDR／cost、NAS、正式 TLS chain、NVS、BUSY、GPIO5、deep sleep、方向、ghosting、六色顯示或功耗。

## PR #53 final closure addendum

本次最後收尾另外要求：ESP32 formal Config 使用 bounded binary A/B blob、generation、CRC、active pointer 與 `sched_txn` schedule/config recovery journal；slot 與 pointer read-back 會 close writable handle 後以 read-only handle 驗證，pointer commit failure 會保留或恢復 last-good pointer；舊 `journal` 僅作相容 fallback，`dashcfg` formal keys 僅作一次性 migration，retry／preview／display／queue ACK 等旁路 keys 不作 Config source。Hosted CI 以 `firmware-host-contract` 執行純 core failure-injection contract，並保留既有八組 ESP32 profile compile。

Benchmark live metrics 以 `selected_photos`、`attempted_photos`、`schema_valid_photos`、`quality_eligible_photos` 與 `ranking_eligible_photos` 分層；quality／ranking 是 conditional metrics，rate 的零分母為 `null`。成本固定以 attempted photos 為分母，任何 unknown usage 都使完整成本平均為 `null`。Golden manifest 在 Provider construction 前 enforce 單一 technical alias、單一 orientation representation、duplicate id 與 duplicate resolved image fail-closed。Provider Level 3 repair 以 `conservative_attempted_calls` 計數，repair timeout／500／connection reset 也算 attempt，文字 repair 不重送圖片。
- 最終分類只能是 `CODE_READY_REAL_ENV_PENDING`；真實 OpenRouter、NAS、PhotoPainter／ESP32 與長時間現場 soak 一律 `NOT RUN`。

## 靜態交接

完成前只允許執行 `git diff --check`、`git status --short`、`git diff --stat origin/main...HEAD`、`git log --oneline origin/main..HEAD` 與指定 `rg` source checks。Final handoff 必須分開記錄 `PR_HEAD`、`TESTED_MERGE_REF`、`MERGE_GROUP` 與 `EXACT_HEAD_WORKFLOW_RUN`；若沒有 `merge_group`，明確寫 `NOT RUN`。PR #53 必須保持 Draft、未 Merge、未 Mark Ready、未 Enable Auto Merge。
