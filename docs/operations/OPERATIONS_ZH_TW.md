# InkTime 操作：決策、Queue 與發布安全

1. 升級前備份 SQLite；目前最高為 Migration 50。Migration 22–24 建立決策／關聯／分析指紋，34–39 加入配對與 12／24 Slot，40–46 加入穩定排序、單調狀態、Idempotency 與 usage retention，47–50 加入 reservation lease、nullable unknown cost、自動 usage retention 與 cleanup audit GC；全部沿用 migration lock、pre-migration backup 與完整性檢查。
2. 在「決策與韌性」確認 Trace 已產生，再依需要開啟 Shadow；關閉 Shadow 後不會有新的背景比較。
3. 為每台 Online 裝置建立 1–14 深度的內容 Queue；Enhanced 裝置依 capability 使用最多 12 或 24 個離線 Slots。確認 Manifest、NVS journal 與 Server 權威 ACK 正常，才依賴離線內容。
4. 人工清理先做 Dry Run，再檢查 `data_cleanup_runs` 與 `data_cleanup_items`；未修改的 `api_usage` 預設政策由 Scheduler 自動分批清理 400 天以前且早於本月的資料。管理員 dry-run 政策只評估不刪除；不應直接刪除 Release 目錄或仍被 Queue Event／LKG／Rollout 引用的 parent。Cleanup audit 本身保留 90 天、每輪最多 GC 10 個終態 run。
5. Canary 先使用一台測試裝置。若出現 `ROLLBACK-001`，保留 health event 與失敗 Release，核對 Last Known Good 後再結束回滾。

管理寫入 API 均需要 administrator、登入 Session 與 CSRF；裝置 Queue API 只接受其自動配對 Device Secret／version 或 Legacy Token。Log 不應包含 Token、Secret、pairing code、照片絕對路徑或完整 EXIF。
