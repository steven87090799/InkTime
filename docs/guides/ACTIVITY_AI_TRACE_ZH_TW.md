# Activity、工作與 AI Trace 判讀

## 已完成，但沒有模型文案

工作 `completed` 不等於曾送出模型請求。先以同一 Job ID／Photo ID 核對：

1. 「工作」`/jobs`：工作類型、策略、建立時間、完成／失敗筆數。本機掃描與 `local` 工作可正常完成但沒有 AI 文案。
2. 「設定」`/settings`：administrator 搜尋「分析執行模式」或 `analysis.execution_mode`。若找不到，清除其他篩選後搜尋完整 key。新安裝 `local_only` 不會呼叫 Provider；一般模型工作需 `automatic_ai`。`local_with_manual_ai` 只開放明確的手動 AI 操作。
3. 「模型與 API」`/providers`：確認啟用、憑證、Provider 專屬模型、Schema 能力與價格；OpenRouter 模型需完整前綴。連線測試成功只代表連線能力。
4. 「AI 分析追蹤」`/ai/traces`：依 Job／Photo／Provider／model 查詢 attempts 的 request、response、parse 與完成時間，再核對「成本」`/costs` 的 `api_usage`。
5. 已存在的工作會保存分析計畫；改模式或模型後先建立少量新工作，不推定舊完成工作會自動重跑。預篩排除、相同內容繼承及 cache hit 也可能沒有新的付費請求。

## Queue 有待處理項目，但沒有工作中的 Worker

先記下出現時間與 Job ID，在「活動紀錄」`/activity`、工作詳細頁及「診斷」`/diagnostics` 比對 Queue、heartbeat、租約及最近錯誤。短暫的領取間隔、Worker 啟動中、長時間 Provider 等待及 Worker 停止需要不同處理，不能只憑一句告警判定。

對照目前部署使用的 Compose 檔，讀取 Worker／Scheduler 最近的日誌與 restart／OOM 狀態。`scripts/container_health.py` 只檢查目標程序是否存在；容器 healthy 不證明每個 Job 有進度。先確認三服務共用同一個 `/data`、版本相符，再處理權限、磁碟、Provider 熔斷或預算原因。不要先清 Queue、重設 DB 或反覆重送可能已計費的工作。

Activity 增量 API `GET /api/v1/activity?after=<next_cursor>` 的游標包含四種事件來源。零筆新事件保留既有畫面；新事件先顯示提示，按提示才加入時間軸。暫停畫面、隱藏分頁或取消自動更新只控制輪詢，不會暫停背景工作。下載遮蔽日誌需要 administrator。

## AI Trace 的證據與權限

- API：`GET /api/v1/ai/traces`，支援 `status`、`provider`、`model`、`job_id`、`photo_id`、`stage`、`trace_id`，以 `before_id` 分頁；每次預設 50、最多 100。
- 詳細頁：`/ai/traces/<trace_id>`；JSON 為 `/api/v1/ai/traces/<trace_id>`。
- Run 狀態為 `RUNNING`、`SUCCESS`、`FAILED`、`TIMEOUT`、`AMBIGUOUS`；attempt 另可為 `VALIDATION_FAILED`。Vision 與 `json_repair` 分開記錄，requested model 與 served model 不一定相同。
- administrator 可查看經遮蔽、截斷的 request／response 與結果；viewer 只取得允許的摘要。遮蔽不保證模型文字不含個人內容，仍不可公開分享完整 trace。
- `TIMEOUT`／`AMBIGUOUS` 不代表供應商沒有處理或計費。用 Provider request ID、`api_usage_id` 與供應商帳務進一步確認，再決定是否重試。
- Trace 屬有界可觀測性資料，Migration 51 的預設保留期為 30 天。缺少 trace 不證明從未分析，可能是歷史資料、清理、cache 或未走同步 Vision 路徑；Batch 應另查 Batch／Item 與 usage。

本文件描述原始碼契約，不宣稱目前部署有無待處理告警。更多操作見[Log 指南](../operations/LOGGING_GUIDE_ZH_TW.md)及[疑難排解](../operations/TROUBLESHOOTING_ZH_TW.md)。
