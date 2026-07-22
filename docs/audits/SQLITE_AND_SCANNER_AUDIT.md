# SQLite 與照片掃描安全審計

審計範圍僅涵蓋 SQLite、Migration、照片掃描、Missing／搬移／重複、縮圖與備份還原。AI 評分、排程介面、Renderer、Firmware 與歷史今日頁面不在本階段。

## 現況

- `Database.connect()` 已統一啟用 WAL、Foreign Key、10 秒 busy timeout 與 `synchronous=NORMAL`；多步驟 Repository 寫入多半使用 `BEGIN IMMEDIATE`。
- `schema_migrations` 已依版本執行交易式 Migration，既有資料庫升級前會建立 SQLite 線上備份。
- `PhotoScanner` 以 generator 走訪目錄，並以路徑、大小、mtime 跳過未變照片；SHA-256 相同且舊路徑不存在時會沿用照片 ID。
- `photos.sha256` 是內容雜湊；`duplicate_group_id` 保存重複照片關係；分析、最愛與顯示資料均以照片 ID 關聯。
- 縮圖位於檔案系統，SQLite 不存放圖片 Blob。
- 備份目前包含 SQLite 與 manifest，但尚無正式還原流程。

## 確認存在的問題

1. 掃描每找到一張照片就查一次 `photos`，預處理後又逐張查詢與提交，形成 N+1 SQL 與大量短交易。
2. 沒有持久化 scan run、掃描模式、last-seen、Missing 原因／時間、取消或 reconciliation 狀態；掛載失敗與大量檔案消失也沒有 10% 安全閥。
3. 單張照片錯誤只增加記憶體計數，沒有保存 stage、錯誤碼、例外型別、可否重試與遮蔽路徑。
4. 搬移判斷依逐張即時查詢；缺少以完整掃描結果為邊界的恢復、Missing 與人工確認流程。
5. 縮圖使用可預測且共用的 `.tmp` 名稱，沒有 single-flight／檔案鎖、輸出驗證與可靠的失敗清理。
6. Migration 沒有 `running/completed/rolled_back` 歷史；程序在 schema commit 前後中斷時，啟動端無法明確辨識未完成狀態。成功後也只由外部命令執行 quick check。
7. 現行備份包含加密 secrets，manifest 未保存資料庫雜湊、Schema version 與重要資料表筆數；沒有「先安全副本、驗證、原子替換、失敗自動回復」的離線還原。
8. 一般 Log 的結構化敏感欄位會遮蔽，但純文字 exception/message 尚無已知 API Key 的精確替換保護。

## 本階段修改計畫

- 保留既有 API 與 `photos.status` 分析流程，另加 `lifecycle_status`、Missing 欄位、metadata/local-feature 完成狀態、scan run 與 scan error 資料表。
- 以 1,000 筆磁碟批次、最多 500 筆寫入交易做批次查詢、記憶體比對與 `executemany`；以 keyset／批次 SQL reconciliation，不載入全庫。
- 對 scanner 與其他 SQLite 寫入提供跨程序單一 Writer 鎖；讀取仍使用獨立 WAL 連線。
- 只有在根目錄可讀、完整走訪成功、未取消、無重大 I/O 錯誤且 Missing 比例未超過預設 10% 時自動 reconciliation；超限把候選照片 ID 保存於 scan 結果並等待管理員確認，較舊掃描不得覆寫新狀態。
- SHA-256 相同時先以舊路徑是否仍存在區分搬移與 duplicate；搬移只更新路徑與 last-seen，不清除分析、Metadata、最愛、顯示歷史或縮圖。
- 以唯一暫存檔、跨程序鎖、JPEG／尺寸驗證與 `os.replace()` 建立縮圖。
- Migration 加入狀態歷史、版本防降級、完整 rollback 與成功後 `integrity_check`；偵測到 `running` migration 時停止平台初始化。
- 備份預設移除 secrets，加入版本、SHA-256 與重要表筆數；提供只能在所有 InkTime 程序停止後執行的離線還原，失敗時自動回復安全副本。
