# InkTime 裝置離線 Queue 協定

本頁是 Online Queue wire schema v1，與 Config Store v5 不同。Enhanced PhotoPainter 另使用[offline schedule 與跨日 staged-next](../resilience/OFFLINE_QUEUE_ZH_TW.md)，不讀 generic Online Queue；版本總表見[現行基線](../reference/CURRENT_STATE_ZH_TW.md)。

裝置以自動配對的 `Authorization: Bearer <device-secret>` 加 `X-InkTime-Credential-Version`，或既有 Legacy `Bearer <device-token>`，取得 `GET /api/device/v1/queue/manifest`。Manifest schema version 為 1，包含 queue item、Release、排程、SHA-256、大小、授權下載 URL 與 Last Known Good。

裝置依序回報 `MANIFEST_RECEIVED`、`DOWNLOAD_STARTED`、`DOWNLOAD_COMPLETED`、`HASH_VERIFIED`、`DISPLAY_STARTED`、`DISPLAY_COMPLETED` 或 `DISPLAY_FAILED` 至 canonical `POST /api/device/v1/queue/ack`。每次 payload 必須包含 `queue_item_id`、`event` 與穩定 `idempotency_key`；不可回報其他裝置的 Item。只有 Display Completed 視為成功顯示並更新 Last Known Good。舊 `/api/device/queue/ack` 僅保留相容性。

下載路徑僅適用於 Manifest 中的本裝置 Queue Item。伺服器會校驗 release file 的名稱、大小與 SHA-256；失效、取消或跨裝置 Item 回傳標準 `QUEUE-002`。
