# 2026-09-16 審查修復紀錄

審查基準 `3d01fbd61759010c113945e285b01205486efe94`；實作基底 `09789e8`，包含既有 v5、semantic repair 與 ALDO3 修復。狀態仍為 NOT_READY，直到本次提交的 Hosted CI 通過。靜態檢查不等於 runtime、NAS 或實板驗收。

| 項目 | 修復／證據入口 |
|---|---|
| AI-01、AI-16 | Migration 61 billable_operations；未知請求跨工作停送，回應先保存再解析；test_billable_operations、test_analysis_pipeline |
| AI-02 | usage_complete / tokens_reported 保留缺失，未知費用不可用零 token 自動對帳；test_openrouter_request_policy |
| AI-03、AI-10、AI-15 | 每次付費呼叫及 Batch shard 原子預留；usage ledger 更新工作成本；diagnostic／live benchmark 共用安裝預算；test_budget_calendar |
| AI-04 | 基底已加入 immutable semantic repair guard；現行 v5 正常分析不發第二次文字修復；保留既有 semantic repair regression |
| AI-08 | Batch 原始行持久 staging，usage 先於 schema／stale 判斷；test_batch_analysis_lifecycle |
| AI-09 | cache hit 與相同 SHA 透過共用 finalize 寫回 photo_analysis；test_batch_analysis_lifecycle |
| BK-01、BK-02 | replace 後立即標記 rollback 狀態；--exact-snapshot 跳過 migration；test_backups |
| BK-03 | 同目錄私有暫存、file／directory fsync；既有資料缺金鑰 fail closed；test_persistent_secret |
| FW-01 | CA 保存在 Transport 成員，TLS stop 後才清除；test_esp32_tls_contract；重連／heap churn 實板驗收尚未執行 |
| WK-01 | 正式 worker 本地 IO／render 使用可終止子程序與硬 deadline；test_worker_timeout；實際 NAS 卡死情境尚未驗收 |
| AI-05、AI-06 | 無 structured output 附完整 schema；本地 validator 使用凍結 caption bounds；test_openrouter_request_policy、test_analysis_schema |
| AI-07、AI-19 | 基底 v5 已精簡輸出欄位與有效 caption controls；既有結果保留，不啟動全庫重送 |
| AI-11 | SQLite 先決定 sample window，Python materialization／stat 有上限；單輪最多 100000 候選；test_batch_analysis_lifecycle |
| AI-12 | 保存 finish_reason／refusal／response ID；終止回應保留費用、不發文字修復；test_provider_contracts |
| AI-13 | 運作參數更新保存舊 semantic revision，現有快取不需要改寫；test_provider_router |
| AI-14 | 帳戶範圍 DB quota、在途 token、RPM、concurrency、cooldown；test_provider_quota |
| AI-17 | OpenAI o-series 使用 max_completion_tokens、移除 temperature；不支援的 reasoning／圖片組合送出前拒絕；test_openrouter_request_policy |
| AI-18 | 下載總上限 256 MiB、讀行上限 4 MiB、SQLite staging；test_provider_batch、test_batch_analysis_lifecycle |
| BK-04 | 安全快照 0600；保留最新已驗證點，最多 3 份／30 天／2 GiB（單一最新點可超限）；test_backups |
| DEP-01 | Linux amd64／arm64 Python 3.12 全依賴 wheel hashes、Debian snapshot、發布 SBOM／provenance；test_dependency_policy；實際映像 digest 由發布流程產生 |
| PERF-01 | readiness 使用 bounded schema／heartbeat 查詢；深度 integrity 留在管理診斷；test_health_readiness |
| PERF-02 | 帳期／photo／job 各自索引查詢；test_budget_calendar |
| RET-01、RET-02 | 安裝時區月界線；未知／操作去重明細保留；清理前封存 known cost，保護最新 minimum_items_to_keep；test_resilience_repository |
| SCH-01 | 指定時刻之後補跑，沿用持久 scheduled-backup:date 去重；test_scheduler |
| MAINT-01、MAINT-02 | 單一 logger／更新 cache 註解；停用舊 Router.submit_batch；test_provider_router |

## 未知費用與重送

先停止相關 worker，使用 `python scripts/reconcile_ai_operation.py --database /data/inktime.db` 列出待對帳操作。對帳或確定接受再付費後，才以 `--approve-resend OPERATION_ID --reason '對帳依據'` 明確批准；此操作保留原始未知費用與預留，不視為免單。不得刪除 ledger 或只重置 job lease 來解除停送。

Live benchmark 必須提供 `--database` 指向既有安裝資料庫，共用全域預算。預留是依配置價格與保守 token 估計，無法保證 provider 的實際收費不超出估计；異常費用仍保留並阻止後續呼叫。

## 依賴更新

用 Python 3.12 執行 `scripts/lock_runtime_dependencies.py` 解析兩平台 lock；不安裝應用依賴。更新直接版本時同步更新 locks 與 Dockerfile 的 Debian snapshot 日期。Hosted CI 負責兩平台映像相容性及安全掃描；正式發布提供映像 digest、SBOM 和 provenance。固定輸入不代表宣稱映像位元組完全相同；build timestamp、attestation 等 metadata 仍有差異。

官方契約：[OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[o3-mini modalities](https://developers.openai.com/api/docs/models/o3-mini)、[Debian snapshot](https://snapshot.debian.org/)。
