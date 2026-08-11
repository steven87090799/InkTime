# 疑難排解

| 現象 | 檢查 |
|---|---|
| `/health/ready` 503 | DB、Migration、`/data` 權限、停滯 Worker |
| 照片為 0 | `/photos` Volume、維護工作錯誤碼 `SCAN-001` |
| 工作停在 running | Worker 容器、heartbeat、租約回收、Provider 熔斷 |
| `VLM-003/004` | 模型 JSON Schema 能力；只會修復一次 |
| `BUDGET-001/002` | 每日／每月／工作／單張停止值與 usage |
| 繁中方框／`IMG-002` | 到「渲染」重新選取內建芫荽／霞鶩文楷 TC，或上傳涵蓋短文案所有字元的繁中字型；系統不依賴 PIL 預設字型 |
| 裝置 401 | 自動模式檢查 Device Secret／credential version 與配對狀態；Legacy 模式才檢查 Bearer Token 是否已重生 |
| 裝置不刷新 | Profile、Manifest schema、SHA、Wi-Fi；四色 2bpp 應為 96,000 bytes，六／七色 indexed4 應為 192,000 bytes |
| Queue ACK 重送或下一張不解鎖 | 檢查 NVS ACK journal、Queue Item／version／Release／event identity；Server 未 2xx 接受前不得清除 pending |
| Enhanced 排程回 `DEVICE-008` | 未確認裝置最多 12 Slots；`legacy_ambiguous` 需重新配對或 Repair 取得明確 24-slot capability |
| 建立工作 timeout 後出現 409 | 重用原 scoped Idempotency Key 取回 replay／resume；同 Key 不可搭配不同 payload |
| 完整照片庫回 `IDEMPOTENCY_IN_PROGRESS` | 另一個 caller 正持有 60 秒 reservation lease 並建立 frozen snapshot；使用同一 Key 稍後重試，不要改 Key 並行列舉 |
| `IDEMPOTENCY_RESERVATION_LOST`／`VLM-008` | Owner heartbeat 或 SQLite lease 更新失敗；本次必須停止，不可沿用未取得 ownership 的 snapshot |
| SQLite locked | 確認單一資料 Volume、busy timeout、避免外部程式長交易；DB 不要放未保證 POSIX lock／fsync 的 SMB／NFS |

仍無法處理時下載診斷包與工作匯出結果；不要貼 API Key、完整 Token、Cookie、Session、GPS 或私人路徑。
