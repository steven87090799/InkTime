# 目標架構

```text
Browser / ESP32
       │
       ▼
Gunicorn + Flask API
  ├── Auth / RBAC / CSRF
  ├── Dashboard / Photos / Jobs / Settings
  ├── Providers / Costs / Devices / Diagnostics / Backups
  └── Service layer
          │
          ▼
Repository layer ─── SQLite（WAL）
          ▲             ├── photos / analysis
          │             ├── jobs / job_items / events / errors
Worker（有界佇列）      ├── settings / secrets / history
  ├── Scanner           ├── devices / releases
  ├── Preprocessor      └── api_usage
  ├── Analyzer
  ├── Batch monitor
  └── Renderer
          │
          ├── Thumbnail cache（內容雜湊）
          ├── Versioned atomic releases
          └── VisionProvider
                 ├── OpenAI 即時
                 ├── OpenAI Batch
                 ├── OpenAI 相容端點
                 └── 本地相容端點
```

## 模組責任

- `app/api`：HTTP 格式、輸入驗證與權限，不寫 SQL、不呼叫模型、不做重型圖片運算。
- `app/services`：工作流程、預算、發布、設定與裝置商業規則。
- `app/repositories`：參數化 SQL、分頁與交易。
- `app/providers`：統一 VisionProvider、使用量、重試分類與成本估算。
- `app/workers`：以資料庫租約取得小批工作，支援停止訊號、退避、Rate Limit 與重啟恢復。
- `app/domain`：分析 Schema、本地影像特徵、日期與渲染規則。
- `app/core`：安全、設定、錯誤、Log、Feature Flag 與事件。

## 核心不變條件

- 主要高品質分析以單一圖片請求同時回傳描述、類型、所有分數與短文案。
- 工作項目從資料庫分批 claim，不為 100,000 張照片一次建立 Future。
- 所有具副作用的 Web 操作要求 administrator 與 CSRF。
- 裝置只以 Bearer Token 驗證；資料庫只存雜湊；完整 Token 只顯示一次。
- Release 在暫存目錄完成校驗後以原子 rename 發布，裝置先驗 Manifest 再套用。
- 一般設定、分析策略與成本限制由 Web UI 管理；部署密鑰仍由環境變數注入。

## 可觀測性

每個 Job／照片／Provider 請求都具有可串接 ID。結構化事件寫入資料庫與 JSON Log；相同錯誤以 fingerprint 聚合。`live` 只檢查程序，`ready` 驗證資料庫、Migration、目錄、設定與 Worker，`detail` 僅 administrator 可讀。
