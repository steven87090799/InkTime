# InkTime 100,000 筆效能驗收報告

測試日期：2026-07-17T03:52:26.928544+00:00
環境：macOS-26.5.1-arm64-arm-64bit／Python 3.9.6
測試性質：使用 100,000 筆照片中繼資料與 Mock／本地流程，不呼叫真實模型、不含原始照片解碼時間。

| 指標 | 結果 |
|---|---:|
| 照片數 | 100,000 |
| SQLite 大小 | 87.03 MiB |
| 批次寫入時間 | 17.572 秒 |
| 模擬掃描寫入速度 | 5,691 筆／秒 |
| 第 99,901 筆起 UI 分頁查詢 | 351.89 ms（回傳 60／總數 100,000） |
| 建立 100,000 個持久化 Job Item | 0.986 秒 |
| Job Item 建立速度 | 101,405 筆／秒（6,084,305 筆／分鐘） |
| Job Item 數 | 100,000 |
| Worker 單次 claim | 8（有界上限 8） |
| 重啟租約回收 | 13.28 ms／8 筆 |
| 取消後停止 claim | 通過 |
| 量測期間最大 RSS | 30.50 MiB |
| 最大 RSS 相對基線增量 | 9.06 MiB |
| 測試程序 CPU 時間 | 6.83 秒／牆鐘 19.59 秒（單核心等效 34.9%） |
| 照片索引 | idx_photos_duplicate, idx_photos_captured, idx_photos_modified, idx_photos_phash, idx_photos_sha256, idx_photos_status_id, sqlite_autoindex_photos_2, sqlite_autoindex_photos_1 |
| SQLite 完整性 | ok |

## 驗收判定

- 工作建立採 500 筆批次寫入，Worker 每次只 claim `concurrency × 2`；不建立 100,000 個 Future。
- UI 使用 LIMIT/OFFSET 與索引，不使用 100,000 個 SQL placeholder。
- 租約逾時可回收到 `pending`；取消後 claim 回傳空集合，不會再送新請求。
- WAL、busy timeout、外鍵與正式 Migration 由共用 Database 連線層啟用。

## 已知瓶頸

- 深頁 OFFSET 仍會隨頁數增加成本；百萬級資料建議改用游標分頁或 PostgreSQL。
- pHash 與模糊度是 CPU 工作，實際 NAS 掃描速度受磁碟、網路與圖片解碼影響，不可用本報告的中繼資料寫入速度推估。
- SQLite 適合單主機中小型部署；多遠端 Worker 應切換 PostgreSQL 儲存層。

## 2026-07-22 跨模組補充

- 同月日候選以 500 筆批次走訪，`top_n` 由 SQL 最終分數排序；random／weighted／prefer 模式只保留常數量選中狀態，不再先截 1,000 筆。
- `tests/integration/test_final_review_history.py` 的 100,000 筆是合成 SQLite 中繼資料，並非 NAS 影像解碼、真實 Renderer 或電池量測。
- 同日 100,000 筆合成案例於 macOS/Python 3.9 單獨測試的整體牆鐘為 5.59 秒、peak memory footprint 160.47 MiB；此數字包含建立 SQLite fixture，測試內候選選取另以 `< 8 秒` 門檻驗收，不能視為純 SQL latency。
- `Database.observability()` 現在提供 writer lock 累計/最大等待、busy timeout 次數、WAL bytes 與長交易計數；數值不包含 SQL、Secret 或完整照片路徑。
- Scanner Peak RAM、目標 NAS WAL 最大值與 writer wait、正式預渲染、靜態 BIN 回應與 Wake 分段仍須在目標 NAS／實體裝置量測；CI runner 數字不得當成 NAS 或續航保證。
