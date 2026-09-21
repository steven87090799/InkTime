# 資料生命週期管理

AI Trace 預設保留 30 天；API usage 預設保留 400 天，未修改的初始化政策由 Migration 49 啟用自動清理，管理員自訂政策保留。清理稽核亦有有界 GC；保留期不是永久帳務保存保證，需長期對帳時先安全匯出。Photo Analysis 歷史列使用[獨立安全清理](../operations/PHOTO_ANALYSIS_RETENTION_ZH_TW.md)，需 dry-run digest／明確確認，不由一般 retention 說明推定已刪除。

預設保留 Decision Trace 180 天、候選 60 天、裝置事件 180 天、Queue Event 90 天、Job Log 30 天。Migration 62 將這五種仍保有初始化設定的政策改為自動清理；升級後 Scheduler 會依保留期分批刪除，管理員已修改的政策保留。`POST /api/retention/dry-run` 只寫入預計刪除紀錄；實際執行會遵守各政策的 `dry_run`。`last_run_at` 表示已評估政策，不保證發生刪除。

Shadow 的 30 天政策目前沒有對應的清理 handler，預設維持觀察模式，不能視為已執行自動刪除。Migration 63 會將早期本分支版本誤啟用且仍未經管理員修改的 Shadow 政策恢復為觀察模式。管理員自訂值仍保留，但不會因此產生尚未實作的清理行為。

Trace 有 Release 關聯時不由自動清理刪除；Queue 的有效／已顯示 Release、Last Known Good 與 Canary 診斷資料應以 rollout 結束後的明確作業處理。所有操作需要管理員與 CSRF。
