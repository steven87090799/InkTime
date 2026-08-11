# InkTime 裝置離線 Queue 協定

裝置以自動配對的 `Authorization: Bearer <device-secret>` 加 `X-InkTime-Credential-Version`，或既有 Legacy `Bearer <device-token>`，取得 `GET /api/device/v1/queue/manifest`。Manifest schema version 為 1，包含 queue item、Release、排程、SHA-256、大小、授權下載 URL 與 Last Known Good。

裝置依序回報 `MANIFEST_RECEIVED`、`DOWNLOAD_STARTED`、`DOWNLOAD_COMPLETED`、`HASH_VERIFIED`、`DISPLAY_STARTED`、`DISPLAY_COMPLETED` 或 `DISPLAY_FAILED` 至 canonical `POST /api/device/v1/queue/ack`。每次 payload 必須包含 `queue_item_id`、`event`、Queue／Release identity 與穩定 `idempotency_key`；不可回報其他裝置的 Item。韌體先把事件寫入 crash-consistent NVS journal，只有 Server 2xx 接受目前 Item／version／Release／event identity 後才清除。非權威、mismatch、stale、timeout 或 5xx 均保留 pending，不能解鎖下一張；只有被接受的 Display Completed 視為成功顯示並單調更新 current／Last Known Good。舊 `/api/device/queue/ack` 僅保留相容性。

`POST /api/device/v1/status` 另帶單調 `status_sequence` 與事件時間。Server 會限制未來 timestamp、忽略倒退 sequence，避免 RTC 漂移或晚到封包覆寫最新裝置狀態。

下載路徑僅適用於 Manifest 中的本裝置 Queue Item。伺服器會校驗 release file 的名稱、大小與 SHA-256；失效、取消或跨裝置 Item 回傳標準 `QUEUE-002`。
