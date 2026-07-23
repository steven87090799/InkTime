# 裝置離線內容佇列

管理員以 `POST /api/devices/<id>/queue/generate` 建立深度 1–14 的 Queue，並加入已發布 Release。裝置使用 Device Token 讀取 `GET /api/device/v1/queue/manifest`，再以 Queue Item 專屬 URL 下載；URL、Queue 歸屬、Release Manifest 與 SHA-256 都會驗證。

裝置以 `POST /api/device/queue/ack` 回報 Manifest、下載、雜湊與顯示事件，必須帶不可重複的 idempotency key。只有 `DISPLAY_COMPLETED` 才更新 current／Last Known Good；下載成功不等於已顯示。
