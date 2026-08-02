# InkTime 操作：決策、Queue 與發布安全

1. 升級前備份 SQLite；平台啟動時 Migration 22（決策與韌性）、23（關聯一致性）與 24（分析指紋）均會沿用既有 migration lock、pre-migration backup 與完整性檢查。
2. 在「決策與韌性」確認 Trace 已產生，再依需要開啟 Shadow；關閉 Shadow 後不會有新的背景比較。
3. 為每台裝置建立 1–14 深度的內容 Queue，確認 Manifest 與裝置 ACK 正常，才依賴離線內容。
4. 清理一律先做 Dry Run，再檢查 `data_cleanup_runs` 與 `data_cleanup_items`；不應直接刪除 Release 目錄。
5. Canary 先使用一台測試裝置。若出現 `ROLLBACK-001`，保留 health event 與失敗 Release，核對 Last Known Good 後再結束回滾。

管理寫入 API 均需要 administrator、登入 Session 與 CSRF；裝置 Queue API 只接受其 Device Token。Log 不應包含 Token、Secret、照片絕對路徑或完整 EXIF。
