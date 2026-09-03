# 疑難排解

新安裝預設本機選片；啟用 AI 與查詢完整證據請依[Activity／AI Trace 指南](../guides/ACTIVITY_AI_TRACE_ZH_TW.md)。不要以刪資料庫、清 Queue 或重送未確認帳務作為第一個修復步驟。

| 現象 | 檢查 |
|---|---|
| `/health/ready` 503 | DB、Migration、`/data` 權限、停滯 Worker |
| 照片為 0 | `/photos` Volume、維護工作錯誤碼 `SCAN-001` |
| 已完成卻沒有 AI 文案／費用 | 檢查 `analysis.execution_mode`、工作 `strategy`、預篩／繼承／cache；以 AI Trace attempts 與 usage 判定是否送出模型請求 |
| Queue 有待處理項目，但沒有工作中的 Worker | 比對同一時間的 Job heartbeat、租約、Worker restart／OOM、Scheduler 與 `/data` 掛載；容器 healthy 只證明程序存在 |
| OpenRouter `CONFIG_INVALID` | Provider 模型欄位必須使用完整 `provider/model` ID，留白的全域 fallback 也須符合；不會自動補前綴 |
| 工作停在 running | Worker 容器、heartbeat、租約回收、Provider 熔斷 |
| `VLM-003/004` | 模型 JSON Schema 能力；只會修復一次 |
| `BUDGET-001/002` | 每日／每月／工作／單張停止值與 usage |
| 繁中方框／`IMG-002` | 到「渲染」重新選取內建芫荽／霞鶩文楷 TC，或上傳涵蓋短文案所有字元的繁中字型；系統不依賴 PIL 預設字型 |
| 裝置 401 | 自動模式檢查 Device Secret／credential version 與配對狀態；Legacy 模式才檢查 Bearer Token 是否已重生 |
| 裝置不刷新 | Manifest Profile／SHA／Wi-Fi；四色 96,000 bytes，六／七色 192,000 bytes；實際 BUSY 與畫面另驗證 |
| SQLite locked | 確認單一資料 Volume、busy timeout、避免外部程式長交易 |

仍無法處理時下載診斷包與工作匯出結果；不要貼 API Key、完整 Token、Cookie、Session、GPS 或私人路徑。
