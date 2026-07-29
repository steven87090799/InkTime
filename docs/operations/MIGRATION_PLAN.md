# InkTime 2.x 遷移與回滾計畫

## 原則

1. 不修改或刪除舊 `photo_scores` 表；新平台先建立並行 Schema。
2. 每次正式 Migration 在交易前，以 SQLite backup API 建立一致備份並執行 `quick_check`。
3. Migration 依 `schema_migrations.version` 單向套用；任何 SQL 失敗立即回滾該版本並停止應用程式啟動。
4. 內容匯入採可重入批次，以相片庫 ID、相對路徑與 SHA-256 保持冪等。

## 升級步驟

1. 停止舊分析腳本與渲染 cron，保留 Web 唯讀服務。
2. 備份 `photos.db`、`config.py`、輸出目錄與韌體設定；原始照片不在應用程式備份範圍。
3. 執行 `python scripts/migrate.py --database <既有資料庫>`。
4. 建立管理員並將舊一般設定匯入 `settings`，API Key 加密匯入 `secrets`。
5. 背景匯入器分批讀取 `photo_scores`，計算相對路徑與內容指紋後建立 `photos`；原表保留供舊渲染器讀取。
6. 升級 ESP32 取得每台裝置 Token 並改用 `/api/device/v1/releases/latest`。確認所有裝置後才關閉舊版模式。
7. 啟動 Web、Worker、Scheduler，先用小型 Mock Provider 工作驗證，再恢復正式排程。

## 設定對應

- `IMAGE_DIR` → `libraries.root_path`
- `API_CHANNELS` → Provider 設定與加密 Secret
- `TIMEOUT`、並行數、模型 → 分析／Provider 設定
- `MEMORY_THRESHOLD`、`DAILY_PHOTO_QUANTITY`、`FONT_PATH` → 渲染設定
- `DOWNLOAD_KEY` → 不匯入新裝置 Token；僅在管理員明確開啟舊版不安全模式時保留

## 回滾

1. 停止 InkTime 2.x 的 Web、Worker 與 Scheduler。
2. 將升級前備份複製到另一個路徑並執行 `PRAGMA quick_check`。
3. 以備份替換服務使用的資料庫，恢復舊 `config.py` 與舊映像檔／Git Commit。
4. 將 ESP32 Server URL 暫時切回舊 API；此舉會恢復 URL 金鑰風險，僅限隔離網路與短期處置。
5. 不刪除新版 `releases/`、快取或診斷資料，直到確認回滾穩定；它們不影響舊版資料庫。

## 風險控制

- 若照片根目錄改變，先更新 library root 再掃描；比對 SHA-256 後搬移既有分析，不以字串 `LIKE '/path%'` 清除。
- 若 2bpp 韌體尚未普及，Release Manifest 依裝置能力提供舊格式；不覆寫固定舊檔。
- Migration 備份不含原始照片，部署者仍需使用 NAS 快照或既有照片備份制度。
