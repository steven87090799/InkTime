# 資料生命週期管理

預設保留 Decision Trace 180 天、候選 60 天、Shadow 30 天、裝置事件 180 天、Queue Event 90 天、Job Log 30 天。`POST /api/retention/dry-run` 只寫入預計刪除紀錄；`POST /api/retention/run` 才會分批刪除。

Trace 有 Release 關聯時不由自動清理刪除；Queue 的有效／已顯示 Release、Last Known Good 與 Canary 診斷資料應以 rollout 結束後的明確作業處理。所有操作需要管理員與 CSRF。
