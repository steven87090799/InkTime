# 資料生命週期管理

AI Trace 預設保留 30 天；API usage 預設保留 400 天，未修改的初始化政策由 Migration 49 啟用自動清理，管理員自訂政策保留。清理稽核亦有有界 GC；保留期不是永久帳務保存保證，需長期對帳時先安全匯出。Photo Analysis 歷史列使用[獨立安全清理](../operations/PHOTO_ANALYSIS_RETENTION_ZH_TW.md)，需 dry-run digest／明確確認，不由一般 retention 說明推定已刪除。

預設保留 Decision Trace 180 天、候選 60 天、Shadow 30 天、裝置事件 180 天、Queue Event 90 天、Job Log 30 天。`POST /api/retention/dry-run` 只寫入預計刪除紀錄；`POST /api/retention/run` 才會分批刪除。

Trace 有 Release 關聯時不由自動清理刪除；Queue 的有效／已顯示 Release、Last Known Good 與 Canary 診斷資料應以 rollout 結束後的明確作業處理。所有操作需要管理員與 CSRF。
