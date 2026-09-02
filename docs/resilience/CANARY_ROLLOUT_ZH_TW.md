# Canary 發布與回滾

Canary 適用相容的 Online 裝置；Enhanced `inktime_offline_schedule` 不由 generic rollout targeting，需依 offline schedule snapshot 交付。詳細 ownership／ACK 邊界見[Queue、Canary 與 LKG](DEVICE_QUEUE_AND_ROLLOUT_ZH_TW.md)。

建立活動後，`POST /api/rollouts/<id>/start` 只將第一個 Stage 的少量裝置加入高優先 Queue，不覆寫既有正式 Assignment。狀態由 repository 集中轉換，避免 API 任意改寫字串；操作會記入 `rollout_actions`。

Queue 回報兩台顯示失敗時會記錄 health event、取消尚未下載的 Canary Queue Item 並進入 `ROLLING_BACK`（`ROLLBACK-001`）。離線裝置不會阻塞其他目標。管理員仍可暫停、核准、繼續或立即回滾。
