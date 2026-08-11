# InkTime 結構化 Log 與問題追蹤指南

## 1. 架構與責任

InkTime 只有一套 stdout structured logging contract，入口是
`inktime/app/core/logging.py`。Docker `json-file` 負責 live runtime 診斷與輪替；
資料庫的 activity/job event、job error 與 Diagnostics 則保存 durable operational
history。兩者用途不同，DEBUG 不會因為開啟 stdout log 而大量寫入 SQLite。

Logging 是 best-effort：formatter、redaction 或輸出失敗不得反向造成 production
operation 失敗。應用 log 與 HTTP access log 分開；access log 預設關閉，避免 health、
static 與裝置輪詢造成重複 I/O。

## 2. Level 契約

| 層級 | 使用時機 | 例子 |
|---|---|---|
| `DEBUG` | 開啟後可重建 decision/control flow | branch、cache hit/miss、claim、lease、scheduler decision、provider phase、pointer phase |
| `INFO` | 低頻且重要的 lifecycle | platform/worker/scheduler start-stop、job start-end、scan、backup、release publish、recovery |
| `WARNING` | operation 可繼續但已 retry/fallback/degrade | 429/5xx、timeout、cache corruption、stale lease、invalid pointer、ACK rejection |
| `ERROR` | operation 無法正確完成 | terminal item failure、DB transaction、provider terminal failure、release/backup failure |
| `CRITICAL` | consistency/security boundary 不安全，程序不應繼續 | unsafe migration/schema、DB integrity、release compensation failure |

不要把每張照片成功、每個 health request、scheduler idle tick、queue poll、lease renew
success 或一般 DB query 放在 INFO。高頻失敗只記前幾筆與週期取樣；完整聚合資料留在
Web「錯誤中心」。

## 3. Standard fields（schema version 1）

每筆 JSON log 都有固定欄位；沒有資料時使用空字串、`0`、`false` 或空物件，讓既有
`jq`/dashboard 查詢保持相容。

| 類別 | 欄位 |
|---|---|
| 基本 | `schema_version`, `timestamp`, `level`, `component`, `event`, `message`, `error_code` |
| Correlation | `trace_id`, `request_id`, `operation_id` |
| Job/worker | `job_id`, `job_item_id`, `worker_id` |
| Domain entity | `photo_id`, `batch_id`, `batch_item_id`, `release_id`, `device_id`, `queue_id`, `queue_item_id` |
| Provider | `provider`, `provider_id`, `model`, `provider_request_id` |
| Scheduler/phase | `task_key`, `schedule_id`, `stage`, `phase`, `operation` |
| Retry/timing | `attempt`, `retry_count`, `duration_ms`, `retryable`, `ambiguous` |
| Transport/failure | `http_method`, `http_status`, `failure_class` |
| Process | `process_role`, `pid`, `thread_name` |
| Exception | `exception_type`, `exception_message`, `stack_trace` |
| Bounded metadata | `details` |

`details` 只放小型、低 cardinality、可安全序列化的 metadata。formatter 會限制深度、
項目數與字串長度；不得放 request/response body、headers、prompt、JSONL、圖片 bytes、
完整路徑或 provider specification。

## 4. Correlation context

- HTTP：接受符合 `[A-Za-z0-9._:-]{1,64}` 的 `X-Request-ID`；其他值由 server 產生，
  response 一律回傳 `X-Request-ID`。`request_id` 同時成為該 request 的 `trace_id`。
- Worker：`job_id`、`job_item_id`、`photo_id`、`worker_id` 與 `attempt` 綁在每個
  ThreadPool item 的 `ContextVar` context，thread/async context 間不共用 mutable state。
- Provider/render/release/queue：沿用上游 context，再加入自己的 provider request、
  release 或 queue identity。explicit event field 會覆蓋 context default。
- 所有 bind 都必須在 `finally`/context manager 清除；不得把 credential 放入 context。

## 5. Component / event catalog

新事件一律使用 snake_case；既有 event 保留以免破壞 dashboard/test。下列為 runtime
主要 canonical event family：

| Component | 主要事件 |
|---|---|
| platform/http | `bootstrap_started`, `runtime_config_resolved`, `migration_check_started`, `service_graph_ready`, `platform_ready`, `bootstrap_failed`, `request_received`, `request_completed`, `request_rejected`, `request_failed` |
| worker/job | `worker_job_execution_started`, `worker_execution_mode_selected`, `worker_claim_started`, `worker_claim_completed`, `worker_claim_empty`, `worker_lease_renewed`, `worker_lease_renew_failed`, `worker_thread_timeout_detected`, `worker_thread_cooperative_shutdown`, `worker_late_completion`, `worker_item_retry_scheduled`, `worker_item_terminal_failure`, `worker_item_budget_exceeded`, `worker_shutdown_requested`, `worker_shutdown_drain_started`, `worker_pause_acknowledged`, `worker_job_finalize_started`, `worker_job_finalize_completed` |
| runner/scanner | `job_dispatch_selected`, `job_processor_branch_selected`, `job_started`, `job_progress`, `job_item_failed`, `job_finished`, `scan_started`, `scan_mode_selected`, `scan_root_validated`, `scan_batch_committed`, `scan_file_metadata_failed`, `scan_file_decode_failed`, `scan_thumbnail_failed`, `scan_missing_safety_triggered`, `scan_cancelled`, `scan_completed` |
| scheduler | `scheduler_started`, `scheduler_task_evaluated`, `scheduler_task_not_due`, `scheduler_task_catchup_decision`, `scheduler_task_high_load_deferred`, `scheduler_task_enqueued`, `scheduled_task_enqueued`, `scheduler_step_failed`, `scheduler_stopped` |
| database/migration | `db_writer_wait_slow`, `db_writer_lock_timeout`, `db_transaction_slow`, `db_transaction_failed`, `db_integrity_failed`, `migration_lock_acquired`, `migration_noop`, `migration_backup_started`, `migration_backup_completed`, `migration_started`, `migration_integrity_check_completed`, `migration_completed`, `migration_rolled_back`, `migration_unknown_schema`, `migration_unfinished_detected`, `migration_history_failed` |
| provider/analysis | `provider_route_started`, `provider_candidate_evaluated`, `provider_candidate_skipped`, `provider_candidate_cooldown`, `provider_candidate_selected`, `provider_failover_started`, `provider_route_exhausted`, `provider_request_prepared`, `provider_request_started`, `provider_response_received`, `provider_rate_limited`, `provider_server_error`, `provider_timeout`, `provider_connection_error`, `provider_protocol_error`, `provider_invalid_json`, `provider_schema_error`, `provider_request_completed`, `analysis_started`, `analysis_cache_hit`, `analysis_cache_miss`, `analysis_cache_invalid`, `analysis_cache_reservation_wait_started`, `analysis_cache_reservation_long_wait`, `analysis_cache_reservation_acquired`, `analysis_provider_request_started`, `analysis_provider_request_completed`, `analysis_provider_request_failed`, `analysis_json_repair_started`, `analysis_json_repair_completed`, `analysis_json_repair_failed`, `analysis_validation_failed`, `analysis_persistence_started`, `analysis_persistence_completed`, `analysis_persistence_failed`, `analysis_completed` |
| batch | `batch_upload_started`, `batch_upload_completed`, `batch_upload_ambiguous`, `batch_submit_started`, `batch_submitted`, `batch_submit_ambiguous`, `batch_poll_started`, `batch_poll_completed`, `batch_cancelled` |
| render/release | `render_started`, `render_candidate_selected`, `render_profile_selected`, `render_completed`, `render_failed`, `release_publish_started`, `release_validation_completed`, `release_db_stage_started`, `release_db_staged`, `release_pointer_snapshot_created`, `release_pointer_activation_started`, `release_pointer_activated`, `release_db_publish_started`, `release_published`, `release_publish_failed`, `release_compensation_started`, `release_pointer_restored`, `release_compensation_completed`, `release_compensation_failed`, `release_reconcile_started`, `release_payload_missing`, `release_orphan_detected`, `release_pointer_invalid`, `release_pointer_recovered`, `release_reconcile_completed` |
| device/queue | `device_request_authenticated`, `device_auth_failed`, `device_release_requested`, `device_release_served`, `device_status`, `queue_manifest_generated`, `queue_manifest_release_skipped`, `queue_ack_received`, `queue_ack_identity_mismatch`, `queue_ack_non_authoritative`, `queue_ack_duplicate`, `queue_ack_committed` |
| maintenance/external | `backup_started`, `backup_completed`, `backup_failed`, `backup_pruned`, `cleanup_started`, `cleanup_completed`, `notification_webhook_request_started`, `notification_webhook_delivered`, `notification_webhook_failed`, `notification_webhook_retry`, `weather_cache_hit`, `weather_request_started`, `weather_request_completed`, `weather_request_failed`, `diagnostics_probe_failed` |
| auth/settings | `login_success`, `login_failed`, `account_locked`, `session_invalidated`, `csrf_rejected`, `authorization_denied`, `settings_update_started`, `settings_updated`, `settings_update_rejected`, `settings_restart_required` |
| firmware | `firmware_boot`, `nvs_factory_reset`, `configuration_missing`, `wifi_connect_started`, `wifi_connected`, `wifi_connect_timeout`, `manifest_download_started`, `manifest_validation_failed`, `frame_cache_hit`, `payload_download_completed`, `payload_download_failed`, `display_refresh_started`, `display_refresh_completed`, `display_refresh_failed`, `device_status_acknowledged`, `sleep_scheduled`, `sleep_time_fallback` |

目前 checkout 沒有 multiprocessing `process_boundary.py` 與獨立 Batch lifecycle service；
worker 是 bounded ThreadPool，Batch transport 位於 OpenAI-compatible provider。不要在查詢時
假設不存在的 child-process event 一定會出現。

## 6. Security / redaction

Redaction 會遞迴處理 dict/list/tuple、exception message、stack trace、URL query 與
`details`。敏感鍵至少包括 `api_key`/`apikey`、`token`、`secret`、`password`、
`authorization`、`cookie`、`session`、`csrf`、`pairing_code`、`device_secret`；
Bearer/Basic credential 與 URL 中的 `token`、`key`、`secret`、`signature` 也會遮蔽。

`job_id`、`release_id`、`device_id`、`request_id` 不會被過度遮蔽。已在 runtime
註冊的完整 secret 也會從純文字 exception 移除。ERROR/CRITICAL 才在 Human formatter
顯示 bounded stack；JSON exception 欄位永遠有長度上限。

## 7. Sampling 與成本

- `should_log_sample(index, first=N, every=N)`：不保存 per-entity state，適合 scanner、
  worker item failure 與 batch progress。
- `should_log_rate_limited(key, interval_seconds=N)`：最多保存 256 個 bounded key，超過時
  移除最舊項目，不會隨任意 key 無限成長。
- analysis、provider routing/transport 與 per-photo render 的正常 DEBUG phase 使用不含 entity ID
  的固定 key 與一秒時間窗取樣；WARNING/ERROR 與 lifecycle state change 不受此取樣抑制。
- expensive metadata 必須放在 `LOGGER.isEnabledFor(logging.DEBUG)` 後計算；不得為了 log
  額外讀圖、hash payload、列舉整個照片庫或 dump SQL parameter。

## 8. 設定與 Docker

在「設定 → Log 與診斷」修改 `system.log_level` 與 `system.log_format`。Web 立即生效；
Worker/Scheduler 下一次 idle poll 生效。Docker 預設每服務 `5 MiB × 3`，三服務最大約
45 MiB；`process_role` 會分別標記 `web`、`worker`、`scheduler`。外接
Loki/Vector/Fluent Bit/NAS Log Center 時仍保留本地輪替。

```bash
docker compose logs --since=1h --no-color
docker compose logs --since=1h inktime-worker
docker compose logs --since=24h --no-color | grep -E '"level":"(warning|error|critical)"'
```

## 9. Troubleshooting recipes

以下先將 Docker JSON log 的應用 payload 交給 `jq`；部署層前綴不同時，可先用 `grep`
縮小範圍。

1. HTTP request：從 response `X-Request-ID` 查完整 request。

   ```bash
   docker compose logs --since=1h --no-color | grep 'req-123' | jq -c 'select(.request_id=="req-123")'
   ```

2. Job：

   ```bash
   docker compose logs --since=24h --no-color | jq -c 'select(.job_id=="job-123") | {timestamp,level,event,job_item_id,photo_id,error_code,duration_ms}'
   ```

3. Photo：

   ```bash
   docker compose logs --since=24h --no-color | jq -c 'select(.photo_id=="photo-123")'
   ```

4. OpenRouter/provider request：

   ```bash
   docker compose logs --since=24h --no-color | jq -c 'select(.provider_request_id=="upstream-123" or (.provider=="OpenRouter" and .event|startswith("provider_")))'
   ```

5. Batch：

   ```bash
   docker compose logs --since=7d --no-color | jq -c 'select(.batch_id=="batch-123")'
   ```

6. Release：

   ```bash
   docker compose logs --since=7d --no-color | jq -c 'select(.release_id=="release-123") | {timestamp,level,event,phase,error_code,duration_ms}'
   ```

7. Device：

   ```bash
   docker compose logs --since=24h --no-color | jq -c 'select(.device_id=="device-123")'
   ```

8. Queue ACK：

   ```bash
   docker compose logs --since=24h --no-color | jq -c 'select(.queue_item_id=="item-123" and (.event|startswith("queue_ack_")))'
   ```

9. Scheduler：

   ```bash
   docker compose logs --since=24h inktime-scheduler --no-color | jq -c 'select(.component=="scheduler") | {timestamp,level,event,task_key,job_id,error_code}'
   ```

10. DB lock/transaction：

    ```bash
    docker compose logs --since=24h --no-color | jq -c 'select(.event=="db_writer_lock_timeout" or .event=="db_writer_wait_slow" or .event=="db_transaction_failed")'
    ```

11. Migration：

    ```bash
    docker compose logs --since=7d --no-color | jq -c 'select(.component=="migration" or (.event|startswith("migration_")))'
    ```

12. ESP32 download/display：Serial monitor 只看 bounded lifecycle event，先找
    `manifest_download_*` → `payload_download_*` → `display_refresh_*` →
    `device_status_*` → `sleep_*`。正式預設 `INKTIME_LOG_LEVEL=2`；短期需要 branch-level
    資訊才用 build flag `-DINKTIME_LOG_LEVEL=3 -DINKTIME_DEBUG_LOG=1`，完成後恢復 INFO。

## 10. 建議排查順序

1. 「診斷」看 Web RSS、cgroup、Queue、WAL、磁碟與 photo library readable 狀態。
2. 「錯誤中心」看聚合 error code、occurrences 與 terminal job item。
3. 用 request/job/release/device ID 查 JSON log timeline。
4. 查 provider request ID、retryable/ambiguous、HTTP status 與 duration。
5. Release 問題依 validation → DB stage → pointer → DB publish → compensation 查找。
6. Queue 問題依 manifest → ACK received → authority/version → commit 查找。
7. 只在必要時短期開 DEBUG，重現一次後立即回 INFO。
