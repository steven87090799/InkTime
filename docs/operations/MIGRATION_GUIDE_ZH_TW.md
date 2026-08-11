# 從舊版 InkTime 遷移

1. 停止舊分析與 cron，備份 `photos.db`、`config.py`、輸出與裝置設定。
2. 執行 `python scripts/migrate.py --database <photos.db>`；舊 `photo_scores` 不會刪除。
3. 啟動新版並建立管理員，再執行下列舊設定匯入工具。
4. 由「維護」掃描照片；SHA-256 可在路徑移動後保留結果，相同內容建立繼承來源。
5. 升級 ESP32 韌體；新自製板依自動配對 request／實體配對碼／管理員核准／可恢復 claim-confirm 取得 Device Secret，既有 Legacy 裝置才建立相容 Token；驗證 Manifest 後才移除舊 URL 金鑰。
6. 用小型本地／Mock 工作驗證，再恢復大量分析。

回滾：停止三服務、驗證 pre-migration 備份、恢復舊 DB／映像／config，短期切回舊韌體。舊 API 有明確安全風險，只可在隔離網路使用。詳見 `MIGRATION_PLAN.md`。

## 匯入舊 `config.py`

先用 dry-run 確認範圍，再正式寫入：

```bash
python scripts/import_legacy_config.py ./config.py --database data/inktime.db --data-dir data --dry-run
python scripts/import_legacy_config.py ./config.py --database data/inktime.db --data-dir data
```

工具會匯入時區、渲染門檻、顯示數量、字型、舊 API 開關與 `API_CHANNELS`。API Key 直接以目前 `session.key` 加密，不會輸出到 Console；若尚無該檔案，請先啟動一次或設定 `INKTIME_SECRET_KEY`。

`DOWNLOAD_KEY` 不會轉成新版 Device Secret 或 Legacy Token。新自製板依實體配對碼與 claim-confirm 流程完成綁定；只有既有 Legacy 裝置才在裝置頁逐台建立相容 Token；舊 API 維持預設關閉。

## Migration 25：帳號正規化與 Session 撤銷

Migration 25 會為既有使用者補上 `normalized_username`、`session_version=1` 與 `disabled_at`，並建立正規化帳號唯一索引。原本的 `username COLLATE NOCASE UNIQUE` 已阻止 ASCII 大小寫碰撞；若升級資料仍違反新索引，Migration 會完整 rollback 並保留升級前備份，不會刪除或覆寫帳號。

## Migration 26：OpenAI Batch 照片分析生命週期

Migration 26 新增 `analysis_batches` 與 `analysis_batch_items`、Batch Usage／價格欄位、`photos.never_upload`／`never_display` 與必要索引。升級前仍由既有流程建立 SQLite 備份、單一交易套用並執行 `integrity_check`；重要表筆數驗證與 25→26、Fresh Database 測試都在 CI 執行。不要手動修改 Migration 1～25；若升級失敗，依備份還原指南回復升級前檔案。

## Migration 27：人工 Review、PhotoPainter 相容與離線排程

Migration 27 新增照片 Review 日期欄位、`photo_reviews`／`photo_review_events`、`analysis_request_outcomes`，以及 PhotoPainter Enhanced 的 `device_offline_schedules`／`device_offline_schedule_slots` 與 Queue 欄位。它也建立 Review 篩選、分析結果、離線 Slot 與完整 SHA-256 對帳所需索引；既有照片的最新分析會初始化為可供人工 Review 的資料。升級仍必須先備份、以單一交易套用並執行 `integrity_check`；Migration 26→27、Fresh Database 與回滾失敗保留資料測試由 CI 覆蓋。不要手動修改 Migration 1～26；若升級失敗，依備份還原指南回復升級前檔案。

## Migration 28、29、30：離線佇列所有權與顯示指標

Migration 28 將 Enhanced schedule snapshot、Slot identity、offline prefetch 權限與端點投影固定下來；Migration 29 重新建立相關 Queue／Slot／rollout 表，將 `offline_schedule_id` 外鍵改為 `ON DELETE RESTRICT`，並保留資料與既有 ownership triggers。Migration 1～30 是 immutable，不能回頭修改。

Migration 30 新增 `device_content_queues.current_displayed_at` 與 `last_known_good_displayed_at`，讓 `DISPLAY_COMPLETED` 以實際顯示時間排序；也會把既有不相容 active delivery 取消並寫入 audit event，建立中央與 SQLite 層的 delivery-mode guard。Enhanced 裝置不得建立 `online_queue`，非 Enhanced 裝置不得建立 `offline_schedule`。

## Migration 31：固定 delivery mode 與 offline prefetch invariant

Migration 31 不修改 Migration 1～30。它先以單一 transaction 修正既有 contradictory devices：`inktime_offline_schedule` 設為 `offline_prefetch_allowed=1`，`legacy_online`／`stock_compat` 設為 `0`；再建立 devices INSERT／UPDATE trigger，拒絕 `DEVICE-008` 矛盾組合。既有 Queue、Queue event、rollout、migration history 與 audit 不會刪除或重建。API 省略 prefetch 欄位時依模式自動正規化，Repository 也會再次檢查。

Migration 31 的 Fresh、30→31、pre-migration backup、restore/restart、`PRAGMA foreign_key_check`、`PRAGMA integrity_check`、contradictory row repair 與 trigger INSERT／UPDATE 都必須由 CI 驗證；失敗仍依既有 migration history 與備份 rollback safety 停止啟動。

升級流程必須涵蓋 Fresh Database、29→30、30→31、升級前 SQLite backup、restore、restart、`PRAGMA foreign_key_check` 與 `PRAGMA integrity_check`。任何 Migration 失敗都會在同一交易 rollback；重新啟動會檢查 migration history，發現未完成標記時停止寫入，應使用 pre-migration backup 還原。

升級前建立的 Session 沒有 `session_version`，因此升級後會失效一次並要求重新登入。之後停用／重新啟用帳號、變更或重設密碼、變更角色都會遞增版本並立即撤銷既有 Session。舊帳號與舊密碼仍可登入；新建帳號與變更密碼才套用 3–64 字元帳號與 12–128 字元密碼規則。

## Migration 32：Provider options 與可追溯成本指標

Migration 32 不修改 Migration 1～31。它為 `providers` 增加受控的 `options_json`，讓正式 OpenRouter routing／privacy policy 與相容端點設定以 canonical JSON 保存；Provider capabilities 仍由 server-side kind policy 計算，不接受用戶端偽造 Batch／reasoning 能力。

同一 Migration 也為 `api_usage` 增加 `cache_write_tokens`、`cost_source`、`prompt_chars`、`schema_chars`、`request_body_bytes` 與 `image_bytes`。`cost_source` 只能是 `provider_reported`、`estimated` 或 `unknown`；unknown 成本不會在預算、成本頁或照片摘要中被當作零。既有 usage row 由 schema default 保留可讀性，新 row 必須寫入明確來源。

升級前仍必須建立 SQLite backup，Migration 以單一交易套用並執行
`PRAGMA foreign_key_check`、`PRAGMA integrity_check`。Fresh Database、31→32、失敗 rollback、重啟與 production release schema gate 必須由 current PR merge-ref CI 驗證；本機不執行測試或建置。

## Migration 33：Provider 身分、OpenRouter legacy 修復與成本回溯

Migration 33 為 `api_usage` 增加可為 null 的 `provider_id` 外鍵，只有 Provider name 唯一時才回填歷史 usage，避免同名 Provider 誤綁；並建立 Provider／model／cost、Job 與 Photo 的 unknown reconciliation 索引。既有 `openai_compatible` 且 host 為 `openrouter.ai` 或其正式子網域的 Provider 會在同一 transaction 轉成 `kind=openrouter`、停用 Batch、保留既有 options，並補上 `require_parameters=true` 預設；非正式 host 不會被轉換。

Migration 33 不會把 `cost_source='unknown'` 的 historical row 推論成 `estimated_cost=0`；Migration 32 之後新增的零值 token、prompt／schema／request／image bytes 欄位不能證明 request 免費。無 billable evidence 的 unknown 可由 budget policy 避免計入 billable reserve，但 provenance 仍保持 `unknown`；有 evidence 的 unknown 等待完整價格後 reconciliation。它不會把缺乏 Provider provenance 的歷史 `actual_cost` 改標為 `provider_reported`。升級前仍必須建立 SQLite backup，並由 hosted CI 驗證 32→33、fresh、idempotency、foreign-key／integrity check 與 rollback；本機不執行測試或建置。

## Migration 34、35：自動配對與 possession-verified credential

Migration 34 加入 automatic／legacy／stock 認證模式、配對狀態、版本化 Device Secret、pending request 與 credential lifecycle；Migration 35 收斂為實體六位數 possession proof、可恢復 claim／confirm、repair permission 與 confirm 後才建立正式裝置列。舊 credential-less automatic row 會停用；不會把 Legacy／Stock 強制轉成 automatic。

## Migration 36～39：Enhanced 離線排程、ACK 與 12／24 Slot capability

- Migration 36 加入 offline schedule version ACK、最小 Slot 間隔與 `first_display_lead`／`fixed_daily` 同步策略。
- Migration 37 保存離線排程 terminal outcome；Migration 38 保存裝置宣告的 Slot 上限。
- Migration 39 將能力收斂為安全的 `unknown_12`、明確 `confirmed_24` 或隔離的 `legacy_ambiguous`，並建立下一次預取截止與 SQLite triggers。舊資料超過 12 Slots 但沒有可信 capability 時不會被猜成 24，必須重新配對或 Repair。

## Migration 40～43：穩定分頁、單調狀態與 request fingerprint

- Migration 40 為照片列表建立穩定排序索引；Migration 41 加入裝置狀態單調 sequence，避免晚到狀態倒退。
- Migration 42 為昂貴 POST 的 Job 保存 `request_fingerprint`，相同 Idempotency Key 的不同 payload 必須衝突。
- Migration 43 修復診斷快取歷史來源。它沒有 SQL statement，而是在同一 Migration transaction 執行有界資料修復；不可因 statements 為空就刪除或重新編號。

## Migration 44～46：可見警告、用量保留與全庫 Idempotency ledger

- Migration 44 為既有 `legacy_ambiguous` 裝置補上可見 `DEVICE-008` 隔離事件，且以 `NOT EXISTS` 保持重入安全。
- Migration 45 為 `api_usage` 加入預設 400 天、batch 200、先 dry-run 的保留政策。
- Migration 46 建立 `idempotency_requests`，保存 scope、request fingerprint、frozen request snapshot、進度與 replay response，讓完整照片庫工作可 conflict／resume／replay 而不重複副作用。

## Migration 47～50：Reservation ownership、unknown cost 與有界 retention

- Migration 47 為 `idempotency_requests` 加入 `reservation_token` 與 `reservation_expires_at`。完整照片庫操作必須先取得 60 秒 lease 並以 10 秒 heartbeat 續租；只有 owner 能列舉並 freeze snapshot，並行 caller 只可 replay／稍後重試，失去 lease 必須停止。
- Migration 48 以交易內 table rebuild 允許同步 `api_usage.estimated_cost` 保存 `NULL`，保留既有 rows、sequence、indexes、triggers 與 FK；unknown 不再被 schema 強迫冒充 0。
- Migration 49 只把 Migration 45 建立、仍完全未修改的 `api_usage` 預設政策由 dry-run 改為自動執行；保留 400 天、每批 200，且不刪除本月資料。管理員改過的政策或仍標 dry-run 的政策不會被強制切換。
- Migration 50 為 `data_cleanup_runs`／`data_cleanup_items` 建立 GC 索引。Scheduler 對完成／失敗的 cleanup audit 保留 90 天、每輪最多刪 10 個 run，且 GC 本身不再新增 audit，避免稽核紀錄無界成長。

目前最高 Schema 是 50。升級工具會鎖定 Migration、只在既有資料庫確有待套用版本時建立 pre-migration backup，並在同一交易內寫 Schema 與完成 history。若啟動看見 running history，只有「Schema 列、名稱與完整性都證明已提交」時才可原子補完 history；其餘狀態以 `MIGRATION-002` 停止並要求離線還原。未知較新 Schema 以 `MIGRATION-003` 停止，history 收尾失敗以 `MIGRATION-004` 停止。

## 歷史 PR #52 跨日修復的資料相容性

該次 staged-next 修復本身沒有新增 SQLite schema；這是歷史提交範圍，不代表目前最高 Schema。明日 schedule 仍以 PhotoPainter SD 上 bounded 的 `staged_next.json`、`.tmp` 與 `.bak` 保存，active schedule 由 snapshot／Queue schema 管理；目前部署仍必須先套用至 Migration 50，並確認韌體 2.8.0 同時支援 `target=current|next`、future rotation、午夜 promote 與 non-terminal prefetch ACK。
