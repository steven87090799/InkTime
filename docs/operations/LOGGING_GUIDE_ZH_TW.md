# Docker Log 與問題追蹤指南

## 設計目標

InkTime 以一套中央結構化 Log 契約串起 Web、資料庫、Migration、Worker、Scanner、Scheduler、Provider、分析、程序隔離邊界、Batch、渲染、Release、裝置 Queue／ACK 與 ESP32 韌體。Log 只能協助追蹤，任何格式化、遮蔽或輸出失敗都不得改變主要操作的成功、失敗、重試或持久化語意。

stdout/stderr 仍由 Docker `json-file` 收集並輪替。應用 Log 與 HTTP access log 分開：應用 Log 可在 Web 動態改層級，access log 為部署層且預設關閉，避免健康檢查與裝置輪詢產生大量無用寫入。本次可觀測性擴充沒有新增資料表或 Migration；目前 Schema 仍依序套用至 Migration 50。

## 層級與雜訊控制

| 層級 | 何時使用 | 正式建議 |
|---|---|---|
| `DEBUG` | Manifest、單次 Provider 候選、低階程序與檔案下載追查 | 短期開啟，完成後改回 INFO |
| `INFO` | 程序啟停、請求／工作／Batch／Release 的重要狀態轉換與完成 | 預設 |
| `WARNING` | 可恢復逾時、Failover、慢交易、拒絕或降級 | 問題期間或極低寫入環境 |
| `ERROR` | 操作失敗、Migration 回滾、資料完整性或 Queue ACK 失敗 | 必須處理 |
| `CRITICAL` | 程序無法安全繼續 | 必須立即處理 |

在「設定 → Log 與診斷」修改 `system.log_level` 與 `system.log_format`。Web 立即生效；Worker 與 Scheduler 在下一次待機輪詢時生效，不必重建映像。

高頻迴圈不逐項輸出 INFO：健康檢查與靜態檔案請求保持安靜；成功照片、Manifest 輪詢與 Provider 候選細節使用 DEBUG、固定取樣或具上限的 rate limit。工作進度仍依 `worker.progress_items` 或 `worker.progress_seconds` 節流，先到者輸出一次。完整聚合錯誤以 Web「錯誤中心」與既有 Activity／Job 資料為準。

## 結構化欄位與關聯 ID

JSON 格式固定包含 `schema_version`、`timestamp`、`level`、`component`、`event`、`message`，並提供下列可查詢欄位；無值時保留空字串、`0`、`false` 或空物件，避免不同程序各自產生不相容格式。

- 關聯：`trace_id`、`request_id`、`operation_id`、`job_id`、`job_item_id`、`worker_id`。
- 領域：`photo_id`、`batch_id`、`batch_item_id`、`release_id`、`device_id`、`queue_id`、`queue_item_id`、`schedule_id`、`task_key`。
- Provider：`provider`、`provider_id`、`model`、`provider_request_id`。
- 生命週期：`stage`、`phase`、`operation`、`attempt`、`retry_count`、`duration_ms`、`failure_class`、`retryable`、`ambiguous`。
- HTTP／程序：`http_method`、`http_status`、`process_role`、`pid`、`thread_name`。
- 例外：`exception_type`、`exception_message`、`stack_trace`、`error_code`、`details`。

Web 接受符合 `^[A-Za-z0-9._:-]{1,64}$` 的 `X-Request-ID`，並在回應帶回同一值；缺少或不合法時產生 32 字元十六進位 ID。請求結束後會清除 context，避免背景工作或下一個請求誤用舊 ID。未經 HTTP 入口的 Web、Worker 與 Scheduler 仍以 `process_role`、工作／Batch／Release ID 關聯。

## 敏感資料與大小上限

所有結構化欄位、巢狀 `details`、純文字訊息與 exception 都經過同一套中央遮蔽與大小限制：

- 遮蔽 API Key、Token、Password、Secret、Authorization／Bearer／Basic、Cookie、Session、CSRF、Credential、配對碼、Wi-Fi 資訊與裝置憑證。
- 遮蔽 headers、payload、body、二進位／Base64 圖片資料、完整私有路徑、精確 GPS，以及 URL 中的 `token`、`key`、`signature` 等敏感 query。
- 保留可安全追蹤的 `request_id`、`job_id`、`batch_id`、`release_id`、`device_id` 等識別碼。
- 字串、exception message、stack trace、巢狀深度與集合項目數都有固定上限；超出部分標示為截斷。
- ESP32 僅記錄固定事件、HTTP 狀態、耗時與列舉結果；不得輸出 Wi-Fi SSID／密碼、Backend URL、配對或裝置憑證。韌體預設 INFO，DEBUG 必須明確編譯開啟。

不得為了除錯新增未經中央遮蔽的 `print`、`Serial.print`、完整 request／response body 或模型圖片內容。診斷記錄失敗時採 fail-open：不影響 Provider retry、Batch ingest、分析寫入、Release 發布或 ACK 狀態。

## 主要生命週期事件

| 邊界 | 代表事件 | 判讀重點 |
|---|---|---|
| Bootstrap／HTTP | `bootstrap_started`、`platform_ready`、`request_received`、`request_completed`、`request_rejected`、`request_failed` | 以 `request_id`、status 與 duration 關聯；健康檢查不逐次輸出 |
| DB／Migration | `db_writer_wait_slow`、`db_transaction_failed`、`migration_started`、`migration_rolled_back`、`migration_completed` | 慢寫入會節流；Migration 失敗仍依既有交易與啟動門檻處理 |
| Worker／Scanner／Scheduler | `worker_started`、`job_started`、`scan_started`、`scan_completed`、`scheduler_started`、`worker_shutdown_requested` | 成功項目不逐張輸出；以 job、stage 與聚合 count 判讀 |
| 程序隔離 | `boundary_call_start`、`boundary_call_success`、`boundary_call_timeout`、`boundary_call_error`、`boundary_process_terminated` | 保留 `vision_started`、`request_started` 與 `ambiguous` 語意，不推測是否可安全重試 |
| Provider／分析 | `provider_route_started`、`provider_failover_started`、`provider_call_start`、`provider_success`、`provider_failure` | 不記錄 prompt、圖片、完整 response 或認證內容；保留 provider、model、attempt 與錯誤分類 |
| Batch | `batch_prepare`、`batch_submit_completed`、`batch_poll`、`batch_completed`、`batch_failed`、`batch_result_ingest`、`batch_restart_recovery` | 以 batch ID、狀態與 count 追蹤，不改變既有 restart recovery 與 ingest 語意 |
| 渲染／Release | `render_start`、`render_success`、`render_failure`、`release_publish_started`、`release_published`、`release_compensation_started` | Release compensation 與 current pointer 的既有原子性保持不變 |
| Queue／ACK | `queue_item_enqueued`、`queue_ack_duplicate`、`queue_ack_committed`、`queue_manifest_generated` | 不記錄 queue payload；duplicate ACK 保持冪等，terminal／nonterminal 狀態依現行規則 |
| ESP32 | `firmware_boot`、`wifi_connect_started`、`payload_ready`、`display_refresh_started`、`display_refresh_completed`、`sleep_scheduled` | 實機 BUSY、顏色、方向、殘影與耗電仍須實體驗收，Log 不能取代硬體證據 |

## 查詢範例

```bash
docker compose logs --since=1h --no-color
docker compose logs --since=1h inktime-worker
docker compose logs --since=24h | grep '"request_id":"req-123"'
docker compose logs --since=24h | grep '"batch_id":"batch-123"'
docker compose logs --since=24h | grep -E '"level":"(warning|error|critical)"'
docker inspect inktime-inktime-worker-1 --format '{{json .State.Health}}'
```

Docker 預設每服務 `5 MiB × 3`，三服務最大約 45 MiB。若接 Loki、Vector、Fluent Bit 或 NAS Log Center，仍保留本地輪替，避免收集端故障時填滿磁碟。

## 問題處理順序

1. 「診斷」看 cgroup、Web RSS、Queue、WAL、磁碟與照片路徑。
2. 「錯誤中心」看聚合錯誤碼與次數。
3. 「工作詳細」確認狀態、完成／失敗、Batch 與是否達預算。
4. 「裝置」確認韌體、RSSI、Heap／PSRAM、最後錯誤與下載成功率。
5. 由 request、job、batch、release 或 device ID 查對應容器最近 30 分鐘 Log；不要先開全域 DEBUG。
6. 需要時暫時改 DEBUG、只重現一次、匯出已遮蔽診斷包，隨即改回 INFO。

## 常見判讀

| 現象 | 類別 | 處理 |
|---|---|---|
| `DEVICE-MANIFEST-HTTP` | ESP32 到 Web 的網路／權限／尚未發布 | 查 HTTP code、DNS、反向代理；不要輸出 Token |
| `DEVICE-DOWNLOAD` | 檔案長度或 SHA-256 失敗 | 查 Wi-Fi RSSI、代理快取、發布檔 |
| `VLM-001` | Provider 逾時／網路 | 依 `ambiguous` 與 `request_started` 判讀，避免不安全重送 |
| `VLM-002` | Rate Limit | 降低並行並等待 Retry-After |
| `JOB-002` | 租約逾時 | 查 Worker restart／OOM；系統會依既有機制回收 |
| 容器 `OOMKilled` | 記憶體上限 | concurrency／queue=1，確認大圖，再調 Worker memory |
| CPU 閒置仍固定跳動 | 輪詢／外部健康檢查過密 | 調高 Web `worker.poll_seconds`；查 NAS probe，不要加入逐輪詢 INFO |
