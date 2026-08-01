# OpenAI Batch 照片分析操作指南

InkTime 的 Batch 是「一行一個獨立 Chat Completions Request」，不是把 100 張照片塞進同一個 Prompt。每一行都有匿名 `custom_id`，結果檔可能亂序，匯入一定以 `custom_id` 對應 SQLite 的 Batch Item；不以輸出順序或檔名對應照片。

## 正式生命週期

1. 建立 Analysis Job 與凍結 Analysis Plan。
2. 本機保守預篩，排除不適合、人工永久排除、`never_upload`、不存在或不在 Library Root 的原始檔。
3. 以內容 SHA 去重，檢查目前 Analysis Fingerprint、Vision Request Fingerprint 與 AI Cache。
4. 在 SQLite 先建立 `analysis_batches`、`analysis_batch_items` 與匿名 `custom_id` 對照並 Commit。
5. 逐張從 ThumbnailCache 產生 EXIF transpose、1024px、JPEG Quality 88 的無 EXIF/GPS 縮圖，串流寫入 `/data/batches/<batch-id>/<shard-id>/input.jsonl`。
6. 上傳 Input File、建立 24h Batch 後立即釋放提交 Worker；`output_expires_after` 使用 `anchor=created_at`，秒數限制在 3600–2592000；Scheduler 只輪詢，Worker 只處理有界 Import 工作。
7. 遠端 terminal state 後下載 Output/Error File，逐行驗證、對帳、使用既有評分與儲存邏輯匯入。
8. 成功、失敗、漏件、Stale 與 Schema 無效分開統計；最後刪除遠端三種 File。刪除失敗只進 `cleanup_pending`，不回滾已匯入結果。

## 100 張 Sample、全庫與增量

在「Batch 照片分析」頁先選「測試 100 張」，確認候選數、Cache 命中、SHA 去重、JSONL 大小、分片數、Token、成本與 Analysis Fingerprint，再提交。候選 Snapshot 與 Sample Seed 會保存於 Batch。

`all_eligible_missing_analysis` 只送所有目前 eligible 且沒有目前 Fingerprint 成功結果的照片；`new_or_changed` 使用相同的增量缺件規則；`manual_selection` 只接受管理員明確指定的 Photo ID。每週重新排名只使用已保存的分析與本機分數，不會重新呼叫模型，也不會做 Stage Two。

## 狀態、取消、過期與重試

本機狀態包含 `preparing`、`uploading`、`validating`、`in_progress`、`finalizing`、`import_pending`、`importing`、`completed`、`completed_with_errors`、`failed`、`expired`、`cancelling`、`cancelled` 與 `cleanup_pending`。若建立 Batch 的 POST 逾時或回應遺失，會以 `last_error_code=submission_unknown` 保留為「提交結果未知」，不會自動重送。

取消或過期時仍先匯入已完成成功行；明確 Error File 行記錄遠端錯誤；未出現於 Success/Error 集合的項目進 `missing`／`retry_pending`。重試只建立失敗、漏件、Schema 無效與可重試項目的新 Batch，不重送已匯入成功項目。

## NAS RAM 與磁碟

安全預設是每片最多 500 requests、150 MiB，且遵守 OpenAI 50,000 requests／200 MB 硬限制。JSONL 以實際寫入 bytes 切片；單行超限會 Fail Closed。每次只保留一張縮圖、Base64 與 JSONL line，沒有 `requests: list`、完整 `lines` 或 `"\n".join(lines)`。

`/data/batches` 必須掛載持久化 Volume；不可用 `/tmp` 保存大型 Input/Output。Batch Detail 會記錄建立 JSONL 時的峰值 RSS 與每片實際大小。完成匯入後遠端 Input/Output/Error File 都會清理；本機檔案依 `batch.local_retention_days` 清理。

## 隱私與資料邊界

JSONL、metadata 與匿名 ID 不含原始檔名、relative path、絕對路徑、GPS、EXIF、相簿名稱、人物姓名、API Key 或 Provider Secret。`never_upload` 與 `never_display` 分離；設定 `never_upload` 不刪除既有分析，也不改變原始照片唯讀狀態。Log 只記 Batch／Item／Job ID、數量、狀態與錯誤碼。

## 成本、Cache 與恢復

Provider 頁可設定標準 Input、Cached Input、Output、Batch Multiplier 或 Batch 專用價格；預設管理值為標準 0.20／0.02／1.20 USD 每百萬 Token、Batch 倍率 0.5。提交前依估算停止超過 Job Budget 的新分片；實際匯入以每個 Batch Item 的 API Usage 記帳，避免同時記 Batch 聚合與逐張 Usage。

`batch.reasoning_effort` 預設為 `none`，可選 `none`、`low`、`medium`、`high`、`xhigh`、`max`。只有 Provider 類型為官方 `OpenAI` 且明確支援時才送出 Chat Completions 的 `reasoning_effort`；一般 OpenAI-compatible Provider 不會收到未知欄位。這個值會進入 Analysis Fingerprint 與 Vision Request Fingerprint，改變推理強度會形成新的 Cache 身分。

Docker 重啟後 Scheduler 由 `remote_batch_id` 繼續 Poll；若 Output 已下載則直接從本機檔案繼續；已 `imported` 的 Item、AI Cache 與 Usage 都會跳過重複寫入；`cleanup_pending` 由後續有界 Import 工作重試。資料庫備份包含 Batch 表、Item 對照、Job 與 Usage。

## 故障排除

- `BATCH-PROVIDER-001`：沒有啟用且支援 Batch 的 Provider；到「模型與 API」勾選 Batch、設定 Base URL、Key、模型價格。
- `BATCH-INPUT-TOO-LARGE`：單張縮圖形成的 Request 已超過分片上限；不要無限重試，降低圖片上限或檢查異常檔案。
- `missing_result`／`unexpected_custom_id`／`duplicate_custom_id`：保留 Output/Error File 與對帳狀態，先查 Batch Detail，再只重試失敗項目。
- `stale`：照片在遠端執行期間內容或 Fingerprint 改變；舊結果不會寫入新內容，下一次增量 Batch 會處理目前版本。
- `cleanup_pending`：分析結果已完成，只有遠端 File 刪除失敗；按「重試遠端清理」，不要重送整批。
- `submission_unknown`：先在 OpenAI Dashboard／API 確認既有遠端 Batch，再在 Batch 詳情輸入既有 `remote_batch_id`；Recovery 只綁定既有 Batch，不會再次 POST `/batches`。

## 離線 Payload 驗證

`control_plane_test` 使用 Fake Provider 驗證生命週期、亂序對帳與冪等匯入；`payload_memory_test` 另使用真正的 OpenAI-compatible Request Builder、System Prompt、full JSON Schema、ThumbnailCache 與 100 張 deterministic structured JPEG，驗證每行都有 `data:image/jpeg;base64,`、匿名 `custom_id`、0600 權限、實際 JSONL bytes 與峰值 RSS。Fake lifecycle 的輸出大小不能代表正式圖片 Payload 大小。
