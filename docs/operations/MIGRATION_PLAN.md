# InkTime 2.x 遷移與回滾計畫

## 原則

1. 不修改或刪除舊 `photo_scores` 表；新平台先建立並行 Schema。
2. 每次正式 Migration 在交易前，以 SQLite backup API 建立一致備份並執行 `quick_check`。
3. Migration 依 `schema_migrations.version` 單向套用；任何 SQL 失敗立即回滾該版本並停止應用程式啟動。
4. 內容匯入採可重入批次，以相片庫 ID、相對路徑與 SHA-256 保持冪等。

## 升級步驟

1. 停止舊分析腳本、渲染 cron 與舊 Web；目前 repository 不再提供這些 runtime。
2. 備份 `photos.db`、`config.py`、輸出目錄與韌體設定；原始照片不在應用程式備份範圍。
3. 執行 `python scripts/migrate.py --database <既有資料庫>`。
4. 建立管理員並在 Modern Web 人工核對一般設定；API Key 只透過 Provider 頁加密寫入 `secrets`。
5. 由 Modern 掃描流程建立 `photos`；`photo_scores` 原表只保留歷史資料，不由目前 runtime 讀寫。
6. 升級 ESP32；新自製板依短效配對 request／管理員核准／claim 取得版本化 Device Secret，既有裝置保留 Device API 的相容 Token 路徑，再改用 `/api/device/v1/releases/latest`。
7. 啟動 Web、Worker、Scheduler，先用小型 Mock Provider 工作驗證，再恢復正式排程。

## 設定對應

- `IMAGE_DIR` → `libraries.root_path`
- `API_CHANNELS` → Provider 設定與加密 Secret
- `TIMEOUT`、並行數、模型 → 分析／Provider 設定
- `MEMORY_THRESHOLD`、`DAILY_PHOTO_QUANTITY`、`FONT_PATH` → 渲染設定
- `DOWNLOAD_KEY` → 不匯入新裝置 Device Secret 或相容 Token

## 回滾

1. 停止 InkTime 2.x 的 Web、Worker 與 Scheduler。
2. 將升級前備份複製到另一個路徑並執行 `PRAGMA quick_check`。
3. 以備份替換服務使用的資料庫，回復先前已驗證的映像；目前 repository 不提供 Legacy runtime。
4. 如需回復部署者自行保存的舊裝置端點，只能在隔離網路短期處置，並承擔 URL 金鑰風險。
5. 不刪除新版 `releases/`、快取或診斷資料，直到確認回滾穩定；它們不影響舊版資料庫。

## 風險控制

- 若照片根目錄改變，先更新 library root 再掃描；比對 SHA-256 後搬移既有分析，不以字串 `LIKE '/path%'` 清除。
- 若 2bpp 韌體尚未普及，Release Manifest 依裝置能力提供舊格式；不覆寫固定舊檔。
- Migration 備份不含原始照片，部署者仍需使用 NAS 快照或既有照片備份制度。
