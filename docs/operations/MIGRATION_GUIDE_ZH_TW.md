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

Migration 28 將 Enhanced schedule snapshot、Slot identity、offline prefetch 權限與端點投影固定下來；Migration 29 重新建立相關 Queue／Slot／rollout 表，將 `offline_schedule_id` 外鍵改為 `ON DELETE RESTRICT`，並保留資料與既有 ownership triggers。Migration 1～29 是 immutable，不能回頭修改。

Migration 30 新增 `device_content_queues.current_displayed_at` 與 `last_known_good_displayed_at`，讓 `DISPLAY_COMPLETED` 以實際顯示時間排序；也會把既有不相容 active delivery 取消並寫入 audit event，建立中央與 SQLite 層的 delivery-mode guard。Enhanced 裝置不得建立 `online_queue`，非 Enhanced 裝置不得建立 `offline_schedule`。

升級流程必須涵蓋 Fresh Database、29→30、升級前 SQLite backup、restore、restart、`PRAGMA foreign_key_check` 與 `PRAGMA integrity_check`。任何 Migration 失敗都會在同一交易 rollback；重新啟動會檢查 migration history，發現未完成標記時停止寫入，應使用 pre-migration backup 還原。

升級前建立的 Session 沒有 `session_version`，因此升級後會失效一次並要求重新登入。之後停用／重新啟用帳號、變更或重設密碼、變更角色都會遞增版本並立即撤銷既有 Session。舊帳號與舊密碼仍可登入；新建帳號與變更密碼才套用 3–64 字元帳號與 12–128 字元密碼規則。
