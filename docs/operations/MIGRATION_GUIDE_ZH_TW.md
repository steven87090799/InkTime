# 從舊版 InkTime 遷移

1. 停止舊分析與 cron，備份 `photos.db`、`config.py`、輸出與裝置設定。
2. 執行 `python scripts/migrate.py --database <photos.db>`；舊 `photo_scores` 不會刪除。
3. 啟動新版並建立管理員，再執行下列舊設定匯入工具。
4. 由「維護」掃描照片；SHA-256 可在路徑移動後保留結果，相同內容建立繼承來源。
5. 升級 ESP32 韌體、建立每台 Token、驗證 Manifest 後才移除舊 URL 金鑰。
6. 用小型本地／Mock 工作驗證，再恢復大量分析。

回滾：停止三服務、驗證 pre-migration 備份、恢復舊 DB／映像／config，短期切回舊韌體。舊 API 有明確安全風險，只可在隔離網路使用。詳見 `MIGRATION_PLAN.md`。

## 匯入舊 `config.py`

先用 dry-run 確認範圍，再正式寫入：

```bash
python scripts/import_legacy_config.py ./config.py --database data/inktime.db --data-dir data --dry-run
python scripts/import_legacy_config.py ./config.py --database data/inktime.db --data-dir data
```

工具會匯入時區、渲染門檻、顯示數量、字型、舊 API 開關與 `API_CHANNELS`。API Key 直接以目前 `session.key` 加密，不會輸出到 Console；若尚無該檔案，請先啟動一次或設定 `INKTIME_SECRET_KEY`。

`DOWNLOAD_KEY` 不會轉成新版 Token。請在裝置頁逐台建立 Token；舊 API 維持預設關閉。

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
`PRAGMA foreign_key_check`、`PRAGMA integrity_check`。Fresh Database、31→32、失敗 rollback、重啟與 production release schema gate 必須由 Final-Head CI 驗證；本機不執行測試或建置。

## Migration 33：Provider 身分、OpenRouter legacy 修復與成本回溯

Migration 33 為 `api_usage` 增加可為 null 的 `provider_id` 外鍵，只有 Provider name 唯一時才回填歷史 usage，避免同名 Provider 誤綁；並建立 Provider／model／cost、Job 與 Photo 的 unknown reconciliation 索引。既有 `openai_compatible` 且 host 為 `openrouter.ai` 或其正式子網域的 Provider 會在同一 transaction 轉成 `kind=openrouter`、停用 Batch、保留既有 options，並補上 `require_parameters=true` 預設；非正式 host 不會被轉換。

Migration 33 不會把缺乏 Provider provenance 的歷史 `actual_cost` 改標為 `provider_reported`。升級前仍必須建立 SQLite backup，並由 Final-Head CI 驗證 32→33、fresh、idempotency、foreign-key／integrity check 與 rollback；本機不執行測試或建置。

## PR #52 跨日修復的資料相容性

本次跨日 staged-next 修復不新增 SQLite schema，也不修改既有 Migration；`MIGRATION=none`。明日 schedule 以 PhotoPainter SD 上 bounded 的 `staged_next.json`、`.tmp` 與 `.bak` 保存，active schedule 仍由既有 snapshot／Queue schema 管理。部署時不需重跑資料遷移；需確認韌體版本同時支援 `target=current|next`、future rotation、午夜 promote 與 non-terminal prefetch ACK。
