# OpenAI Batch 照片分析生命週期稽核

稽核基準：`fix/lan-production-finalization`（PR #33，`faeb3c9`）。本文件先記錄
現況，再作為本次持久化 Batch 生命週期實作的邊界。稽核日期：2026-08-01。

## 現有 Batch 能力

- `VisionProvider` 已有 `submit_batch()`、`poll_batch()`、`cancel_batch()` 抽象方法。
- `OpenAICompatibleProvider.submit_batch()` 目前將呼叫端傳入的 `requests: list[dict]`
  全部序列化為記憶體中的 JSONL，再一次上傳 `/files`，接著建立 `/batches`。
- 目前只檢查 1～50,000 筆與 200 MB，沒有依實際寫入 bytes 分片，也沒有保留本機
  input/output/error 檔案。
- `OpenAIBatchProvider` 只有標記 `supports_batch = True`，並未提供持久化流程。
- Provider 已有同步 `/chat/completions`、JSON Schema、usage 解析與基本成本估算；URL
  對 `/chat/completions` 有相容端點例外，尚未正規化為 API Root。
- Web Provider 頁目前明確把 Batch 標示為 Experimental／Provider API only。

## 缺少能力

- 沒有 `analysis_batches`、`analysis_batch_items`、分片與 custom ID 對照資料。
- 沒有 `sample`、`all_eligible_missing_analysis`、`new_or_changed`、`manual_selection`
  的固定候選快照，也沒有 `never_upload` 隱私旗標。
- 沒有與 AI Cache、目前 Analysis Fingerprint、SHA 去重及 active Batch 互斥的候選查詢。
- 沒有低記憶體串流縮圖/Base64/JSONL 寫入、150 MiB/500 筆安全分片或單行超限錯誤。
- 沒有非阻塞的提交後流程：Scheduler poll、terminal import job、串流下載、亂序對應、
  success/error/missing 對帳、stale/schema_invalid、冪等匯入與 cleanup_pending。
- 沒有把 Batch usage 以 `analysis_batch`／`batch` 記帳，也沒有 batch pricing multiplier、
  每千張推估或 Job Budget 的提交前停止線。
- 沒有管理員 Batch API、Batch 詳細頁、取消/重試/遠端檔案清理操作。
- 沒有 Fake Batch Provider、固定 JSONL fixture、重啟/部分成功/漏件/重複輸出等自動化覆蓋。

## 現有資料流與目標資料流

現況同步路徑：

```text
Job -> frozen Analysis Plan -> Job Worker -> PhotoAnalysisService
    -> ThumbnailCache -> Provider / AI Cache -> validate -> score -> photo_analysis
    -> api_usage / job_item
```

目標 Batch 路徑：

```text
Admin API/UI
  -> frozen Analysis Plan + deterministic candidate snapshot
  -> local prefilter / cache / SHA / active-batch filtering
  -> SQLite commit: Job + analysis_batches + batch_items + custom_id map
  -> bounded streaming JSONL shards under /data/batches
  -> upload input file -> create remote Batch -> persist remote IDs -> return

Scheduler (bounded poll only)
  -> validating/in_progress/finalizing/cancelling/cleanup_pending
  -> terminal state -> bounded analysis_batch_import Job

Worker import Job
  -> resumable output/error download
  -> line-by-line parse and custom_id reconciliation
  -> current SHA/fingerprint check
  -> validate + existing score/save/cache/usage transaction
  -> missing/retry accounting -> remote/local cleanup
```

每週排名只使用已保存的 `photo_analysis` 與本機分數，不會進入遠端 Batch 路徑。

## 狀態機

```text
preparing -> uploading -> validating -> in_progress -> finalizing
                                                   -> import_pending -> importing
                                                   -> completed / completed_with_errors
                                                   -> failed / expired / cancelled

任何完成匯入但遠端檔案刪除不完整的 terminal state -> cleanup_pending -> completed
```

取消先進入 `cancelling`，仍匯入已存在的成功/錯誤結果；過期或取消的漏件進入
`retry_pending`，不重送已成功項目。Import 可由已存在的 local output/error 檔案繼續，
每一個 item 以資料庫狀態與唯一 usage key 保證冪等。

## 資料表與既有契約

- 既有 `jobs`／`job_items`：保留一般 Job 租約、重試與權限；Batch 本身另有明確
  `analysis_batches` 主表，terminal import 只建立有界的 `analysis_batch_import` 工作。
- 既有 `photos`：`active`、`eligible`、人工排除與原始檔安全路徑是候選前提；新增
  `never_upload` 與 `never_display` 分離，解除 never_upload 不會刪既有分析。
- 既有 `photo_analysis`：使用原有 `validate_analysis_result()`、`_score_result()`、
  `_save_result()` 等效路徑，寫入 `schema_kind=full`、`stage=single_high`、
  `analysis_source=analysis_batch`。
- 既有 `ai_analysis_cache`：以 `content_sha256 + vision_request_fingerprint` 命中，
  不建立第二次付費請求；Batch Import 成功後沿用相同 cache key。
- 既有 `api_usage`：Batch item 以 `request_type=analysis_batch`、`processing_mode=batch`
  與 `batch_id/batch_item_id/request_id` 擴充欄位記錄，避免 Batch aggregate 與 item
  usage 重複記帳。
- 既有 Provider/Model pricing：價格維持管理介面設定；Batch 使用獨立 multiplier 或
  batch per-million 欄位，不把 API Key 或價格寫死在 Python。
- `analysis_batches` 保存遠端狀態、檔案 ID、local path、token/cost、對帳計數、錯誤與
  cleanup 狀態；`analysis_batch_items` 保存匿名 custom ID、提交時 SHA/Fingerprint、
  response/error 與 import 狀態。

## 主要失敗模式

- connect/read timeout、429/Retry-After、5xx、無效 JSON、缺少遠端 ID、下載中斷、重試
  造成的重複 submit 或重複 import。
- input shard 超過 500 requests/150 MiB 或單行超限；任一單行超限必須明確失敗。
- output/error 亂序、duplicate/unexpected custom ID、同 ID 同時出現在兩檔、無效 JSONL、
  HTTP 200 但缺 response body、Schema 不合法、missing result。
- 照片在提交後變更 SHA 或 Plan 變更 Fingerprint；舊結果標 stale，不寫入新版本。
- Job Budget 在提交前不足；已提交分片仍必須匯入及記帳。
- 遠端 Input/Output/Error File 清理失敗；不可回滾已保存分析，進入 `cleanup_pending`。
- Docker/NAS 重啟、worker 中止於匯入中間、Scheduler 重啟；資料庫狀態與 local files
  必須讓下一個角色繼續而不重新提交整批。

## 隱私邊界

- Batch JSONL 只含匿名 `ibt:<uuid>` custom ID、chat completion body、模型與必要 Schema；
  不含檔名、relative/absolute path、相簿名稱、GPS、EXIF、人物姓名、API Key 或 Secret。
- 圖片只取既有 ThumbnailCache 的 EXIF transpose、`high_image_max_side`（預設 1024）、
  JPEG quality；輸出 JPEG 移除 EXIF/GPS，原始照片掛載與寫入權限不變。
- Log 只記錄 batch/job/item 的匿名 ID、數量、狀態與 error code，不寫 Base64、Prompt
  全文、照片路徑或 Secret；錯誤回應先遮蔽 Secret。
- `never_upload` 是人工管理的上傳禁用旗標，和 `never_display` 語意分離；候選查詢及
  提交前雙重檢查強制排除，既有 `photo_analysis` 不因此刪除。

## NAS RAM／磁碟風險

- 目前 `requests` list、JSONL lines list 與整檔 join 會按照片數放大 RSS；目標改用逐行
  產生、寫入、釋放，僅保留單張縮圖/編碼/line。
- `/data/batches/<batch>/<shard>/input.jsonl` 是持久化工作檔，不使用 `/tmp` 保存大型
  JSONL；檔案 mode 0600，Docker restart 後仍可恢復。
- 安全預設每分片最多 500 筆或 150 MiB，並保留實際 shard bytes、峰值 RSS、cleanup
  時間與保留策略，防止 NAS 磁碟長期被遠端輸入/輸出檔占用。
- 原始 `/photos` 維持唯讀；Batch 暫存、SQLite、cache、backup、release 均在 `/data`。

## 結論與實作邊界

本基底已能安全保存同步分析結果，但 Batch 還停留在 Provider API adapter 層。實作時
保留同步 `/v1/chat/completions` 與既有 Schema/分析評分契約；Batch 僅使用一次
`single_high/full` 完整分析，不新增 Stage One -> Stage Two。所有遠端等待移出 Worker，
由 Scheduler poll 與有界 import Job 驅動，並以 SQLite 明確狀態與 item mapping 作為重啟後
唯一真實來源。
