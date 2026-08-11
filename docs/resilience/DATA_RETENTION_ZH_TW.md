# 資料生命週期管理

預設保留 Decision Trace 180 天、候選 60 天、Shadow 30 天、裝置事件 180 天、Queue Event 90 天、Job Log 30 天。Migration 45 另加入 `api_usage` 400 天、batch size 200 的政策；Migration 49 只將「仍是 Migration 45 原始值、未經管理員修改」的該政策切成自動執行。Scheduler 會分批刪除超過 400 天且早於本月的 usage；管理員明確設為 dry-run 的政策只標記 `skipped` 並更新 `last_run_at`，不刪資料。`POST /api/retention/dry-run` 仍只寫入預計刪除紀錄；`POST /api/retention/run` 才依各政策執行或跳過。

Trace 有 Release 關聯時不由自動清理刪除；Queue 的有效／已顯示 Release、Last Known Good 與 Canary 診斷資料應以 rollout 結束後的明確作業處理。Queue Event child、Rollout、Release、Usage 與其他仍受外鍵／稽核引用的 parent 必須保留；清理會分批 skip／延後，不能先刪 parent 造成孤兒或破壞 ACK provenance。所有操作需要管理員與 CSRF。

Migration 50 為 cleanup audit 建立查詢索引。Scheduler 對 `completed`／`failed` 的 `data_cleanup_runs` 保留 90 天，每輪最多刪除 10 個 run；child items 由外鍵級聯清理，GC 本身不建立新的 cleanup audit，因此不會為了記錄 GC 而無界增加下一輪待清項目。
