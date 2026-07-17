# 疑難排解

| 現象 | 檢查 |
|---|---|
| `/health/ready` 503 | DB、Migration、`/data` 權限、停滯 Worker |
| 照片為 0 | `/photos` Volume、維護工作錯誤碼 `SCAN-001` |
| 工作停在 running | Worker 容器、heartbeat、租約回收、Provider 熔斷 |
| `VLM-003/004` | 模型 JSON Schema 能力；只會修復一次 |
| `BUDGET-001/002` | 每日／每月／工作／單張停止值與 usage |
| 繁中方框 | 上傳涵蓋字元的 CJK TC 字型；勿依賴 PIL 預設字型 |
| 裝置 401 | Bearer Token、啟用狀態、Token 是否已重生 |
| 裝置不刷新 | Manifest 尺寸／2bpp／SHA、Wi-Fi、96,000 bytes |
| SQLite locked | 確認單一資料 Volume、busy timeout、避免外部程式長交易 |

仍無法處理時下載診斷包與工作匯出結果；不要貼 API Key、完整 Token、Cookie、Session、GPS 或私人路徑。
