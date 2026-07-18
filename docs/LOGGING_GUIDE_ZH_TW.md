# Docker Log 與問題追蹤指南

## 設計目標

InkTime 的 stdout/stderr 由 Docker `json-file` 收集並輪替。應用 Log 與 HTTP access log 分開：應用 Log 可在 Web 動態改層級，access log 為部署層且預設關閉，避免健康檢查與裝置輪詢產生大量無用寫入。

## 層級

| 層級 | 何時使用 | 正式建議 |
|---|---|---|
| `DEBUG` | Manifest、檔案下載等低階追查 | 短期開啟，完成後改回 INFO |
| `INFO` | 服務啟停、工作開始／節流進度／完成、備份、每日裝置狀態 | 預設 |
| `WARNING` | 工作含失敗、ESP32 回報異常、可恢復問題 | 問題期間或極低寫入環境 |
| `ERROR` | 取樣後的工作項目失敗、無法完成的操作 | 必須處理 |
| `CRITICAL` | 程序無法安全繼續 | 必須立即處理 |

在「設定 → Log 與診斷」修改 `system.log_level` 與 `system.log_format`。Web 立即生效；Worker 與 Scheduler 在下一次待機輪詢時生效，不必重建映像。

## 不會輸出的內容

- 不輸出 API Key、裝置 Token、Cookie、Session、密碼或完整 Authorization Header。
- 不逐張輸出成功照片。
- 不預設輸出每個 HTTP request、健康檢查或輪詢。
- 項目錯誤只輸出前 3 筆與後續取樣；完整聚合內容在 Web「錯誤中心」。
- 工作進度依 `worker.progress_items` 或 `worker.progress_seconds` 節流，先到者輸出一次。

## JSON 欄位

每筆結構化 Log 包含 `timestamp`、`level`、`component`、`event`、`error_code`、`message`、`job_id`、`photo_id`、`provider`、`model`、`duration_ms`、`retry_count`、`details`。敏感鍵會遞迴遮蔽。

常用事件：

- `platform_ready`、`worker_started`、`scheduler_started`
- `job_started`、`job_progress`、`scan_progress`、`job_item_failed`、`job_finished`
- `backup_completed`
- `device_status`；正常為 INFO，有錯誤碼時為 WARNING

## 查詢範例

```bash
docker compose logs --since=1h --no-color
docker compose logs --since=1h inktime-worker
docker compose logs --since=24h | grep '"event":"job_progress"'
docker compose logs --since=24h | grep -E '"level":"(warning|error|critical)"'
docker inspect inktime-inktime-worker-1 --format '{{json .State.Health}}'
```

Docker 預設每服務 `5 MiB × 3`，三服務最大約 45 MiB。若接 Loki、Vector、Fluent Bit 或 NAS Log Center，仍保留本地輪替，避免收集端故障時填滿磁碟。

## 問題處理順序

1. 「診斷」看 cgroup、Web RSS、Queue、WAL、磁碟與照片路徑。
2. 「錯誤中心」看聚合錯誤碼與次數。
3. 「工作詳細」確認狀態、完成／失敗與是否達預算。
4. 「裝置」確認韌體、RSSI、Heap／PSRAM、最後錯誤與下載成功率。
5. 再查對應容器最近 30 分鐘 Log；不要先開全域 DEBUG。
6. 需要時暫時改 DEBUG、重現一次、匯出已遮蔽診斷包，隨即改回 INFO。

## 常見判讀

| 現象 | 類別 | 處理 |
|---|---|---|
| `DEVICE-MANIFEST-HTTP` | ESP32 到 Web 的網路／權限／尚未發布 | 查 HTTP code、Token、DNS、反向代理 |
| `DEVICE-DOWNLOAD` | 檔案長度或 SHA-256 失敗 | 查 Wi-Fi RSSI、代理快取、發布檔 |
| `VLM-001` | Provider 逾時／網路 | 測試 Provider、降低並行、查端點 |
| `VLM-002` | Rate Limit | 降低並行並等待 Retry-After |
| `JOB-002` | 租約逾時 | 查 Worker restart／OOM；系統會回收 |
| 容器 `OOMKilled` | 記憶體上限 | concurrency／queue=1，確認大圖，再調 Worker memory |
| CPU 閒置仍固定跳動 | 輪詢／外部健康檢查過密 | 調高 Web `worker.poll_seconds`；查 NAS probe |
