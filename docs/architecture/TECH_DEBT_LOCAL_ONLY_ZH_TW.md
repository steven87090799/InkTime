# 本輪延後的技術債

> 本頁保存當時 local-only 交付的延期事項，不是最新待辦清單。是否仍存在須重新比對原始碼；現況見[版本基線](../reference/CURRENT_STATE_ZH_TW.md)。

本機無 AI 選片與雙照片文字版型刻意沒有修改下列高風險項目：RenderService 拆分、NumPy
Dithering 研究、Database Connection Pool、Font Cache、Renderer 效能分析、`_REGISTERED_SECRETS`
FIFO，以及 ManagedConnection 極端例外清理。

舊 Web 入口已退休，正式入口是 `create_app()`。`repair_json()` 已按實際 Stage 使用
`json_schema_for_stage()`；本輪不調整其 Schema。SQLite `Connection.backup()` 已建立一致
Snapshot，本輪不加入 WAL checkpoint。

當時工作區的未追蹤副本是本機資料，不是專案功能契約。現行 `USER_MANUAL.html` 已納入版本控制，是文件入口；其他 worktree 的個人副本仍應保留，不能依本紀錄自動清理。
