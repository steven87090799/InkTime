# InkTime PR #53 Final One-Shot Hardening Audit

## 基線與範圍

| 項目 | 值 |
|---|---|
| Repository | `steven87090799/InkTime` |
| PR | `#53 fix: harden final production and OpenRouter runtime` |
| Base | `origin/main 064a599bd9e13dd95c6e7d326759d8efc9b22306` |
| Start HEAD | `6c7e88703ee09befc728be7ee9a9478c352d759b` |
| Branch | `fix/final-production-openrouter-and-runtime-hardening` |
| Worktree | `/Users/steven/Desktop/inktime/InkTime-final-production-openrouter-runtime-hardening` |
| Delivery state | Draft、Open、未 Merge、未 Mark Ready、未 Enable Auto Merge |

本輪只處理與 PR #53 直接相關的 P1-001 unknown cost reserve／scope blocking／reconciliation、P1-002 legacy OpenRouter effective kind、P2-001 prompt contract cache identity、P2-002 Scoring Lab usage accounting、P2-003~005 ESP32 NVS／sticky session、P3-001~004 redirect／TLS／device HTTP／pricing contracts，以及文件與 hosted exact-head evidence。`ConverTo6c_bmp-7/`、`data/cache/`、`data/releases/`、使用者檔案與其他工作樹未納入修改。

## 驗證政策

依施工手冊，本機禁止 pytest、ruff、mypy、pip install／pip-audit、Docker／Compose、Playwright、Arduino／PlatformIO／ESP32 編譯、runtime soak、production smoke、真實 OpenRouter API、付費呼叫與硬體操作。本機只做 Git、文字檔、差異與靜態契約核對；測試與安全掃描以 GitHub Actions hosted evidence 為準。

## 要求矩陣

| 區域 | Code／契約狀態 | Hosted／真實環境邊界 |
|---|---|---|
| OpenRouter repair privacy、routing、ZDR、usage、sticky policy | CODE STATIC PASS；共用 helper，repair 無 image／reasoning，Vision／repair 共用 context | HOSTED CONTRACT PASS 待 final-head workflow；LIVE PROVIDER `NOT RUN` |
| ESP32 CA 上限與 NVS 寫入／read-back | CODE STATIC PASS；共享 `kMaxDeviceCaPemBytes=3500`、X.509 parse 與 `PAIRING-NVS-006/007` | HOSTED COMPILE／source contract 待 final-head；physical NVS／TLS `NOT RUN` |
| Migration 32/33 Provider 身分與歷史成本來源 | CODE STATIC PASS；legacy OpenRouter data fix、provider_id backfill、舊 actual cost 不標 `provider_reported` | HOSTED migration regression 待 final-head；正式資料 upgrade `NOT RUN` |
| Frozen repair policy、Vision fingerprint 與 prompt contract | CODE STATIC PASS；新 plan 帶 scoring／caption／reasoning／provider revision hash，無 legacy v2 fallback | HOSTED Python contract 待 final-head；既有 production job runtime `NOT RUN` |
| Provider Level 1/2/3 | FAKE CONTRACT PASS；Level 2/3 明確按鈕且不自動執行 | HOSTED fake contract 待 final-head；REAL PROVIDER `NOT RUN` |
| P1-001 unknown cost budget | CODE STATIC PASS；有證據 unknown 以 reserve 計入有效預算，同 photo／job scope block，補價格後回溯 reconciliation | HOSTED budget／pricing contract 待 final-head；真實帳務 `NOT RUN` |
| P1-002 legacy OpenRouter | CODE STATIC PASS；official exact/subdomain host effective kind，Migration 33 persist kind、停 Batch、default `require_parameters=true` | HOSTED migration/provider route contract 待 final-head；REAL PROVIDER `NOT RUN` |
| P2-001 prompt contract cache identity | CODE STATIC PASS；scoring rules hash、caption controls、reasoning、schema、provider revision 與固定 contract version 入 hash | HOSTED cache contract 待 final-head；production cache migration `NOT RUN` |
| P2-002 Scoring Lab usage | CODE STATIC PASS；Vision／repair 分列、成本相加、repair image bytes=0、mixed unknown 顯示 incomplete | HOSTED UI／usage contract 待 final-head；REAL PROVIDER `NOT RUN` |
| P2-01 remote config atomic persistence | CODE STATIC PASS；candidate 先 `saveConfig(candidate)` 並完成 persistence/read-back，成功後才變更 runtime cfg／changed flag；失敗不重啟 | HOSTED firmware/source contract 待 final-head；physical NVS／reboot `NOT RUN` |
| P2-02 Top-K tie bound | CODE STATIC PASS；deterministic exact-K、ID tie-break、`effective_k` bounded，overlap rate 不超過 1；Spearman 保留 average-rank ties | HOSTED benchmark contract 待 final-head |
| P2-03 production ranking reuse | CODE STATIC PASS；benchmark 直接重用 `calculate_ranking_score()`、`DEFAULT_RANKING_WEIGHTS`、`RANKING_RULE_VERSION`，favorite bonus disabled | HOSTED benchmark contract 待 final-head；REAL MODEL QUALITY `NOT RUN` |
| P2-04 golden manifest | CODE STATIC PASS；`inactive`／`ineligible`／`missing`／`never_upload`／`manually_excluded` canonical fields，unknown/type/privacy/path fail-closed 且 network 前排除 | HOSTED benchmark contract 待 final-head；REAL DATASET `NOT RUN` |
| P2-003~005 ESP32 NVS／sticky session | CODE STATIC PASS；legacy cleanup fail 保留新 journal，older-safe pointer fallback，context 不含 path／secret | HOSTED firmware/source contract 待 final-head；physical NVS／reboot／network `NOT RUN` |
| P3-001~004 redirect／TLS／HTTP／pricing | CODE STATIC PASS；scheme/host/port、X.509 parse、literal private HTTP、finite bounded pricing | HOSTED security／pricing contract 待 final-head；real device／TLS chain `NOT RUN` |
| Conditional P2 private HTTP DNS safety | CODE STATIC PASS；generic Python 與 ESP32 LAN policy 僅允許 literal private/loopback IP，hostname 需明確 pinned compile flag；Ollama 無 bypass，redirect disabled | HOSTED security contract 待 final-head；REAL NETWORK `NOT RUN` |
| P3 provider privacy diagnostic | CODE STATIC PASS；snapshot 明確回報 `data_collection=deny/allow/openrouter_default` 與 `zdr=true/false`，未明確 deny+zdr 不標 configured | HOSTED provider contract 待 final-head；REAL PROVIDER `NOT RUN` |
| P3 Level 3 reasoning | CODE STATIC PASS；Level 3 明確使用 `reasoning_effort=none`，不硬 forcing low，能力欄位與 provider adapter 對齊 | HOSTED provider contract 待 final-head；REAL PROVIDER `NOT RUN` |
| P3 max-cost wording／unknown stop | CODE STATIC PASS；bounded post-response stop；可靠成本可阻擋下一 request，unknown 立即停止後續 request 且不填零 | HOSTED benchmark contract 待 final-head；REAL PROVIDER `NOT RUN` |
| 文件與 HTML 索引 | CODE／STATIC PASS；本輪同步 OpenRouter、成本與 ESP32 邊界文件 | 連結／瀏覽器驗證不替代 source-aligned static review |

## Hosted provenance 規則

本報告刻意分開三種證據，不再把 Pull Request merge ref 稱為 exact-head：

- `PR_HEAD`：PR #53 當時 feature branch 的提交。
- `TESTED_MERGE_REF`：`pull_request` workflow 驗證 PR 加上當前 `main` 的合併相容性。
- `EXACT_HEAD_WORKFLOW_RUN`：最後推送後由 PR workflow 驗證 feature branch，且 `headSha == FINAL_HEAD` 的 hosted run；本倉庫目前不以 merge-ref 或舊 run 代替這項證據。

既有 PR checks 是 merge-ref 證據；完成每次 push 後，必須重新取得兩個 exact-head run，逐一確認 `headSha=FINAL_HEAD`、`conclusion=success`。最終 run ID 與 SHA 以 PR #53 Checks 及交接回報為唯一最新證據，避免在文件內留下會漂移的舊 run ID。

## 成本、隱私與硬體邊界

- Migration 33 會回填唯一 Provider name 對應的 `provider_id`，並把 official OpenRouter legacy row 原子轉成 `kind=openrouter`、停 Batch、保留 options；歷史 `actual_cost` 沒有 provider provenance 時不得標為 `provider_reported`。
- Provider Level 2/3 與 live benchmark 只接受 deterministic synthetic／明確 non-private golden manifest；不得讀取 production photo、AI cache、release 或 display history。
- Container evidence 的 Trivy 語意是 `unallowlisted policy findings = PASS`；temporary unfixed CVE allowlist 仍存在，expiry 為 `2026-09-30`，不可寫成 `0 vulnerabilities`。
- hosted CI 的 fake provider、offline benchmark、ESP32 compile 與 source contract 都不能推論真實 OpenRouter routing／ZDR／cost、NAS、正式 TLS chain、NVS、BUSY、GPIO5、deep sleep、方向、ghosting、六色顯示或功耗。

## PR #53 final closure addendum

本次最後收尾另外要求：ESP32 formal Config 使用 bounded binary A/B blob、generation、CRC、active pointer 與 `sched_txn` schedule/config recovery journal；slot、pointer、journal、legacy cleanup 與 clear read-back 會 close writable handle 後以 read-only handle 驗證，pointer commit failure 會保留或恢復 last-good pointer；舊 `journal` 僅作相容 fallback，`dashcfg` formal keys 僅作一次性 migration，retry／preview／display／queue ACK 等旁路 keys 不作 Config source。Hosted CI 以 `firmware-host-contract` 執行純 core failure-injection contract，並保留既有八組 ESP32 profile compile。

Benchmark live metrics 以 `selected_photos`、`attempted_photos`、`schema_valid_photos`、`quality_eligible_photos` 與 `ranking_eligible_photos` 分層；quality／ranking 是 conditional metrics，rate 的零分母為 `null`。成本固定以 attempted photos 為分母，任何 unknown usage 都使完整成本平均為 `null`。Golden manifest 在 Provider construction 前 enforce 單一 technical alias、單一 orientation representation、duplicate id 與 duplicate resolved image fail-closed。Provider Level 3 repair 以 `conservative_attempted_calls` 計數，repair timeout／500／connection reset 也算 attempt，文字 repair 不重送圖片。
- 最終分類只能在 code blockers 關閉、最新 exact-head hosted workflow 全部 success 且 worktree clean 時寫成 `CODE_READY_REAL_ENV_PENDING`；真實 OpenRouter、NAS、PhotoPainter／ESP32 與長時間現場 soak 一律 `NOT RUN`。

## 靜態交接

完成前只允許執行 `git diff --check`、`git status --short`、`git diff --stat origin/main...HEAD`、`git log --oneline origin/main..HEAD` 與指定 `rg` source checks。Final handoff 必須分開記錄 `PR_HEAD`、`TESTED_MERGE_REF`、`MERGE_GROUP` 與 `EXACT_HEAD_WORKFLOW_RUN`；若沒有 `merge_group`，明確寫 `NOT RUN`。PR #53 必須保持 Draft、未 Merge、未 Mark Ready、未 Enable Auto Merge。
