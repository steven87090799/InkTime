# InkTime Legacy 記憶體與資料安全稽核

稽核基準：`origin/main` `7386e11ce36b091e4fde9d3449701b583ca89f5d`。本文件記錄修正前的真實程式狀態；Legacy 僅為相容期維護來源，不是正式權威資料來源。

| 項目 | 分類 | 修正前證據與處置 |
|---|---|---|
| Legacy Review／Simulator 預設 | 仍存在 | `legacy_server.py` 的 `ENABLE_REVIEW_WEBUI` 由舊 `config.py` 讀取且預設 `True`；`server.py` 直接匯入 Legacy app。改為明確環境／app config flag，所有環境預設關閉。 |
| 停用路由一致性與早期阻擋 | 部分已修 | `/review`、`/sim`、`/sim_render`、`/api/md_list`、`/images`、`/files` 已呼叫 `_require_webui_enabled()`，但 flag 預設不安全，且 Legacy renderer 在 import time 載入。統一為 404 並延遲重型 renderer import。 |
| 新版 Dashboard／Rendering／Preview／Device／Health | 不存在 | 這些由新版 Blueprint／服務提供；本修正不變更 ESP32 Bearer Token 或新版路由契約。Legacy URL-key Device API 另有既有預設關閉設定。 |
| 日期清單全庫 EXIF 載入 | 仍存在 | `_load_all_md_list()` 執行 `SELECT exif_json FROM photo_scores` 後 `fetchall()`，再於 Python 解析整庫 JSON。改為新版 `photos.captured_month_day` 的 DISTINCT 有索引查詢。 |
| 日期清單 Cache | 仍存在 | process-global dict 無 lock、singleflight、容量與 stale-on-error；同時 miss 可重複掃庫。改為有界 TTL/LRU、每 key singleflight、有限等待與最後成功 stale。 |
| 新版日期欄位與歷史今日 | 部分已修 | `photos.captured_at` 與其 index 已存在，另有 `substr(captured_at,6,5)` expression index；Scanner 會寫 `captured_at`，但只有單一 EXIF 格式且查詢仍依賴字串切割。新增實體 `captured_date`／`captured_month_day`／解析狀態與 index。 |
| Migration／Backfill | 測試不足 | 現有 Migration 19、交易 rollback、跨 thread migrate、升級前備份及完整性檢查已有測試；沒有日期 materialization 或有界可重複 backfill。新增 Migration 20 與 keyset batch backfill。 |
| Scanner 日期解析 | 部分已修 | Scanner 捕捉無效 EXIF 日期並留 `captured_at=NULL`，但只支援 `%Y:%m:%d %H:%M:%S` 且沒有有界去重警告或可稽核解析狀態。改用共用 parser。 |
| Legacy 日期 parser | 仍存在 | `legacy_server.py` 與 `render_daily_photo.py` 各自以 split／replace 寬鬆解析，可能接受不可能或不完整日期。改用同一 parser。 |
| Legacy review 分頁／排序 | 仍存在 | page size 無硬上限，offset 可任意增長，時間篩選／排序在整表使用 `json_extract`／`substr`。改用白名單、硬上限及 materialized Legacy 欄位；缺欄位時安全降級，不解析 JSON。 |
| Simulator 查詢 | 部分已修 | `/sim` 初始頁已不載入全庫，選定照片時只取 31 天；但 SQL 仍對 JSON 做運算且日期 placeholder 沒有通用批次界線，保留的 `load_sim_rows()` 仍可載入全表。移除全表 helper，日期集合固定分批。 |
| Legacy Analyzer 記憶體 | 仍存在 | `list_images()` 回傳完整 list、`filter_unscored()` 建立任意 placeholder、並行模式一次 submit 全部 Future，並將路徑清單寫至專案目錄。改為 generator、有限 SQL batch 與 `2 * concurrency` 在途 future。 |
| Legacy Analyzer 正式入口 | 已有替代方案但 Legacy 未切換 | 正式 Docker Worker 使用 `inktime.app.workers.runner` 與新版 `PhotoScanner`；Scheduler 與 `server.py` 不呼叫 Analyzer。加入靜態契約測試與 Maintenance-only 標示。 |
| NAS Remount | 仍存在 | `_try_remount_nas()` 不判斷平台即執行 `osascript`、吞掉所有例外、固定輪詢 sleep，log 會輸出完整 NAS URL。限制 Darwin、嚴格 timeout、安全摘要；Linux／Docker 僅做可讀性檢查。 |
| Thumbnail lock | 仍存在 | 每個 `{sha}-{size}` 永久建立一個 `.lock`；雖沒有在 finally 刪鎖，仍會無界累積。改為固定 256 shard lock，保留 flock、atomic replace、fsync 與 validation。 |
| 舊 Thumbnail lock 清理 | 不存在 | 沒有安全離線工具。新增預設 dry-run、明確 `--yes`、不跟 symlink 且只匹配舊 lock pattern 的工具；不在 startup 自動執行。 |
| Backup／Restore | 部分已修 | 已使用 SQLite backup API、manifest checksum、integrity check、restore safety copy 與 runtime exclusive lock；需新增 Migration 20 欄位／backfill 的 round-trip 覆蓋。 |
| SQLite／外部環境 | 外部環境限制 | SQLite 版本與 query planner 會在測試環境實測；實際 NAS 掛載、正式 Docker Volume 與真實照片不在本任務中操作。macOS remount 以 mock 驗證，不使用真實 credential。 |

## 安全邊界

- 不讀寫正式 SQLite、備份、照片、縮圖、Release、Docker Volume 或 NAS 內容。
- 所有容量、Migration、Lock 與 Remount 測試只使用 temporary directory、隔離 SQLite 與合成圖片。
- `photo_scores` 只保留 Legacy 相容，不刪表、不改為正式權威來源，也不呼叫 Provider 或解碼全庫照片做 backfill。

## 實作後控制

- 日期 schema 升至 Migration 20；`photos.captured_date`、`captured_month_day` 與 `capture_date_status` 由 Scanner、人工 metadata update 及每批最多 500 列的可重複 backfill 維護。日期清單固定使用 `idx_photos_captured_month_day`，最多回傳 366 個有效值。
- 日期 cache 以資料庫 resolved path、device/inode 與 library scope 組成 key；TTL 300 秒、最多 16 entries、每 key singleflight、等待上限 2 秒，refresh 失敗保留最後成功值。
- Legacy Analyzer 的掃描／待分析清單存於匿名 temporary file；SQL batch 預設 500、concurrency 硬上限 16、在途 Future 固定 `concurrency * 2`。
- Thumbnail 使用 256 個固定 shard；鎖位於權限 `0700` 的 `.locks/`，檔案權限 `0600`，名稱只含 shard number。新版本不建立逐照片鎖；若發現舊鎖則額外取得它以保護 rolling upgrade 中既有工作。
- 舊鎖只可在 Web、Worker、Scheduler 全停後執行 `python scripts/cleanup_legacy_thumbnail_locks.py <cache-dir> --yes --services-stopped`；未給 `--yes` 一律 dry-run，且 symlink 不列入／不刪除。
- Container／Linux／Windows 只檢查照片根目錄存在且可讀，不執行 host mount；只有 Darwin maintenance analyzer 可在嚴格 timeout 內呼叫絕對路徑 `/usr/bin/osascript`，Log 不記錄 URL、credential、stderr 或私人照片路徑。
