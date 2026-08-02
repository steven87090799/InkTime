# 裝置離線內容佇列

管理員以 `POST /api/devices/<id>/queue/generate` 建立深度 1–14 的 Queue，並加入已發布 Release。裝置使用 Device Token 讀取 `GET /api/device/v1/queue/manifest`，再以 Queue Item 專屬 URL 下載；URL、Queue 歸屬、Release Manifest 與 SHA-256 都會驗證。

正式韌體先讀取 Queue；只有 Queue endpoint 回 404 或 Queue 為空時，才回退既有 Latest Release。Manifest 必須是 bounded JSON object，Queue version、尺寸與大小必須是真正 JSON integer；下載 URL 必須是 Item 綁定的同源相對路徑。Content-Type、Content-Length、實際長度、Profile、尺寸與 SHA-256 任一不符都會 fail closed，保留舊畫面。

裝置以 canonical `POST /api/device/v1/queue/ack` 回報 Manifest、下載、雜湊與顯示事件。每個事件先把穩定 idempotency key 與 payload 寫入 NVS；只有 HTTP 2xx 才清除，timeout／5xx／重新開機會重送同一 Key，409 視為 stale Queue version。401／403 不會降級成匿名或 Latest 成功。只有 `DISPLAY_COMPLETED` 才更新 current／Last Known Good；下載成功不等於已顯示。舊 `/api/device/queue/ack` 僅保留相容路由，不應用於新韌體或新文件。

成功顯示後，韌體在 NVS 保存 SHA-256、Release、render profile、rotation、board profile 與成功標記。下一次只有所有欄位完全相同且狀態完整時才回報 `display_skipped=true` 並跳過面板刷新；forced refresh、rotation／profile／board 改變或 NVS 損壞都不可 skip。
