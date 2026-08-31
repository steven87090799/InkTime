# InkTime 操作：決策、Queue 與發布安全

日常先核對 `/activity`、`/jobs`、`/ai/traces`、`/costs` 與 `/diagnostics`；操作入口與常見誤判見[Activity／AI Trace](../guides/ACTIVITY_AI_TRACE_ZH_TW.md)。

1. 升級前備份 SQLite；平台啟動依序套用至目前 Migration 52，沿用 migration lock、pre-migration backup 與完整性檢查；NAS 更新另由更新器先建立 recovery point。
2. 在「決策與韌性」確認 Trace 已產生，再依需要開啟 Shadow；關閉 Shadow 後不會有新的背景比較。
3. Online 相容裝置可建立深度 1–14 的 Queue；Enhanced 裝置須透過 offline schedule prepare 與 snapshot，不可加入 generic online Queue。確認 Manifest 與真正 display ACK 後才認定交付。
4. 清理一律先做 Dry Run，再檢查 `data_cleanup_runs` 與 `data_cleanup_items`；不應直接刪除 Release 目錄。
5. Canary 先使用一台測試裝置。若出現 `ROLLBACK-001`，保留 health event 與失敗 Release，核對 Last Known Good 後再結束回滾。

管理寫入 API 均需要 administrator、登入 Session 與 CSRF；裝置 Queue API 只接受其自動配對 Device Secret／version 或 Legacy Token。Log 不應包含 Token、Secret、pairing code、照片絕對路徑或完整 EXIF。
