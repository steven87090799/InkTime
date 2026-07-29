# 本輪延後的技術債

本機無 AI 選片與雙照片文字版型刻意沒有修改下列高風險項目：RenderService 拆分、NumPy
Dithering 研究、Database Connection Pool、Font Cache、Renderer 效能分析、`_REGISTERED_SECRETS`
FIFO，以及 ManagedConnection 極端例外清理。

`legacy_server.py` 直接入口問題已過期：正式入口是 `create_app()`。`repair_json()` 已按實際 Stage
使用 `json_schema_for_stage()`；本輪不調整其 Schema。SQLite `Connection.backup()` 已建立一致
Snapshot，本輪不加入 WAL checkpoint。

主要 Repository 的 `ConverTo6c_bmp-7/`、`USER_MANUAL.html`、`inktime/app 2/` 與
`inktime/app/web/templates/simulator 2.html` 是未知本機資料，絕不可由本功能自動處理。
