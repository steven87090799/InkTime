# InkTime 文件地圖

這是 InkTime 文件的單一入口。根目錄的 [`USER_MANUAL.html`](../USER_MANUAL.html) 提供可瀏覽的專案規格與完整文件連結；本頁則提供適合 GitHub、純文字閱讀器與程式碼審查的 Markdown 索引。

## 先從這裡開始

| 你要做什麼 | 先讀 |
|---|---|
| 第一次啟動 Docker | [快速開始](getting-started/QUICK_START_ZH_TW.md) → [安裝指南](getting-started/INSTALLATION_ZH_TW.md) |
| 了解程式如何運作 | [現行架構與流程](architecture/ARCHITECTURE_ZH_TW.md) → [Application Factory](architecture/APPLICATION_FACTORY.md) |
| 管理照片、模型、裝置與設定 | [管理員指南](guides/ADMIN_GUIDE_ZH_TW.md) → [使用者指南](guides/USER_GUIDE_ZH_TW.md) |
| 部署、備份或故障排除 | [Docker 部署](operations/DOCKER_GUIDE_ZH_TW.md) → [備份還原](operations/BACKUP_RESTORE_ZH_TW.md) → [疑難排解](operations/TROUBLESHOOTING_ZH_TW.md) |
| 開發或修改系統 | [開發指南](getting-started/DEVELOPMENT_GUIDE_ZH_TW.md) → [架構文件](architecture/ARCHITECTURE_ZH_TW.md) |
| 接入 ESP32 或 PhotoPainter | [ESP32 指南](devices/ESP32_GUIDE_ZH_TW.md) → [自動配對](devices/ESP32_AUTOMATIC_PAIRING_ZH_TW.md) → [PhotoPainter](devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md) → [交付模式](devices/PHOTOPAINTER_DELIVERY_MODES_ZH_TW.md) |
| 使用本機無 AI 模式 | [Local-only 選片與雙照片](guides/LOCAL_ONLY_SELECTION_ZH_TW.md) |
| 執行 OpenAI Batch 分析 | [OpenAI Batch 照片分析](OPENAI_BATCH_ANALYSIS_ZH_TW.md) → [正式交付與安全交接](PRODUCTION_READINESS_SECURITY_HANDOFF_ZH_TW.md) |
| 設定 OpenRouter 或查看成本來源 | [OpenRouter Provider](providers/OPENROUTER_ZH_TW.md) → [Token 與成本指南](reference/TOKEN_COST_GUIDE_ZH_TW.md) |
| 離線比較模型與解析度 | [Model Benchmark](providers/MODEL_BENCHMARK_ZH_TW.md) |
| 為 ESP32 配置 HTTPS 信任根 | [ESP32 TLS／配網](devices/ESP32_TLS_PROVISIONING_ZH_TW.md) |
| 使用 Decision Trace、Queue 或 Canary | [決策與韌性總覽](resilience/DECISION_FEEDBACK_RESILIENCE_PLAN_ZH_TW.md) |
| 交接 PR #53 的修復與證據 | [Final One-Shot Hardening Audit](reports/FINAL_ONE_SHOT_HARDENING_AUDIT.md) → [Production Readiness Handoff](PRODUCTION_READINESS_SECURITY_HANDOFF_ZH_TW.md) |

## 文件規則

- `getting-started/`：安裝、首次啟動與開發環境。
- `guides/`：依角色或功能使用系統的說明。
- `architecture/`：現行程式結構、設計決策與演進邊界。
- `operations/`：部署、安全、備份、遷移與故障處理。
- `devices/`：ESP32、電子紙、傳輸與硬體驗收。
- `resilience/`：Decision Trace、Shadow、離線 Queue、資料保留與 Canary。
- `reference/`：穩定參考資訊，例如版本記錄與成本規則。
- `reports/`：可重跑的現行量測輸出。
- `archive/`：已完成的稽核與舊基線；它們是歷史證據，不代表目前行為。

## 完整文件索引

### 根目錄與入口

- [`../README.md`](../README.md)：中文專案首頁、完整資料流與快速入口。
- [`../README.en.md`](../README.en.md)：英文 legacy README；僅供舊使用方式參考。
- [`../USER_MANUAL.html`](../USER_MANUAL.html)：可瀏覽的專案規格與所有 Markdown 文件索引。

### Batch 與交付專項

- [`OPENAI_BATCH_ANALYSIS_ZH_TW.md`](OPENAI_BATCH_ANALYSIS_ZH_TW.md)：Batch 輸入快照、隱私、生命週期、成本與實際人工 smoke 邊界。
- [`PRODUCTION_READINESS_SECURITY_HANDOFF_ZH_TW.md`](PRODUCTION_READINESS_SECURITY_HANDOFF_ZH_TW.md)：正式環境安全、LAN、持久化與交接檢查。
- [`branch-consolidation-report.md`](branch-consolidation-report.md)：分支整併、Migration 26、CI 與遠端分支清理證據。
- [`post-merge-hardware-validation.md`](post-merge-hardware-validation.md)：軟體／hosted PASS 與真實 OpenAI、NAS、ESP32 驗收的 NOT RUN 邊界。

### 開始使用

- [`getting-started/QUICK_START_ZH_TW.md`](getting-started/QUICK_START_ZH_TW.md)：Docker 最短啟動路徑與首輪驗收。
- [`getting-started/INSTALLATION_ZH_TW.md`](getting-started/INSTALLATION_ZH_TW.md)：Docker／原生安裝與首次管理員設定。
- [`getting-started/DEVELOPMENT_GUIDE_ZH_TW.md`](getting-started/DEVELOPMENT_GUIDE_ZH_TW.md)：分層規則、開發指令與測試原則。

### 使用指南

- [`guides/ADMIN_GUIDE_ZH_TW.md`](guides/ADMIN_GUIDE_ZH_TW.md)：設定欄位、裝置、排程與管理界面責任。
- [`guides/USER_GUIDE_ZH_TW.md`](guides/USER_GUIDE_ZH_TW.md)：照片、工作、渲染、發布與診斷操作。
- [`guides/API_PROVIDER_GUIDE_ZH_TW.md`](guides/API_PROVIDER_GUIDE_ZH_TW.md)：OpenAI 相容 Provider 與各廠商接入步驟。
- [`providers/OPENROUTER_ZH_TW.md`](providers/OPENROUTER_ZH_TW.md)：正式 OpenRouter kind、routing／privacy options、reasoning、成本與 Batch 邊界。
- [`providers/MODEL_BENCHMARK_ZH_TW.md`](providers/MODEL_BENCHMARK_ZH_TW.md)：offline-contract／live-quality、golden manifest、quality／ranking metrics、解析度／Prompt／reasoning／routing 維度與輸出契約。
- [`guides/EPAPER_SIMULATOR_ZH_TW.md`](guides/EPAPER_SIMULATOR_ZH_TW.md)：虛擬電子紙接收端與無硬體驗收。
- [`guides/LOCAL_ONLY_SELECTION_ZH_TW.md`](guides/LOCAL_ONLY_SELECTION_ZH_TW.md)：`analysis.execution_mode`、本機選片、雙照片與文案來源。

### 架構與設計

- [`architecture/ARCHITECTURE_ZH_TW.md`](architecture/ARCHITECTURE_ZH_TW.md)：現行執行架構、資料流、選片與 Release Coordinator。
- [`architecture/ARCHITECTURE_TARGET.md`](architecture/ARCHITECTURE_TARGET.md)：架構責任與不變條件。
- [`architecture/APPLICATION_FACTORY.md`](architecture/APPLICATION_FACTORY.md)：Web／Worker／Scheduler 的 Factory 與初始化順序。
- [`architecture/RUNTIME_CONFIGURATION.md`](architecture/RUNTIME_CONFIGURATION.md)：RuntimeConfig 與部署設定邊界。
- [`architecture/LEGACY_RETIREMENT_PLAN.md`](architecture/LEGACY_RETIREMENT_PLAN.md)：Legacy 相容與退休門檻。
- [`architecture/VISUAL_ORIENTATION_CORRECTION.md`](architecture/VISUAL_ORIENTATION_CORRECTION.md)：EXIF、AI 與人工視覺方向規則。
- [`architecture/TECH_DEBT_LOCAL_ONLY_ZH_TW.md`](architecture/TECH_DEBT_LOCAL_ONLY_ZH_TW.md)：Local-only 交付刻意未處理的技術債。

### 維運、安全與部署

- [`operations/DOCKER_GUIDE_ZH_TW.md`](operations/DOCKER_GUIDE_ZH_TW.md)：N100 Docker 部署、資源、健康檢查、更新與回滾。
- [`operations/N100_RESOURCE_GUIDE_ZH_TW.md`](operations/N100_RESOURCE_GUIDE_ZH_TW.md)：N100 容量與低功耗調校。
- [`operations/LOGGING_GUIDE_ZH_TW.md`](operations/LOGGING_GUIDE_ZH_TW.md)：Log 層級、遮罩與判讀方法。
- [`operations/TROUBLESHOOTING_ZH_TW.md`](operations/TROUBLESHOOTING_ZH_TW.md)：常見故障與處理順序。
- [`operations/ERROR_CODES_ZH_TW.md`](operations/ERROR_CODES_ZH_TW.md)：穩定錯誤碼與建議處置。
- [`operations/SECURITY_GUIDE_ZH_TW.md`](operations/SECURITY_GUIDE_ZH_TW.md)：登入、CSRF、Secret、Token 與網路安全。
- [`operations/PRODUCTION_PREFLIGHT_ZH_TW.md`](operations/PRODUCTION_PREFLIGHT_ZH_TW.md)：公開 URL、Cookie、Proxy 與 SQLite 掛載預檢。
- [`operations/BACKUP_RESTORE_ZH_TW.md`](operations/BACKUP_RESTORE_ZH_TW.md)：備份內容、驗證與離線還原。
- [`operations/SECRET_RECOVERY_ZH_TW.md`](operations/SECRET_RECOVERY_ZH_TW.md)：加密 Secret 的災難復原 Bundle。
- [`operations/MIGRATION_GUIDE_ZH_TW.md`](operations/MIGRATION_GUIDE_ZH_TW.md)：舊版遷移與 `config.py` 匯入。
- [`operations/MIGRATION_PLAN.md`](operations/MIGRATION_PLAN.md)：升級、回滾與資料相容原則。
- [`operations/OPERATIONS_ZH_TW.md`](operations/OPERATIONS_ZH_TW.md)：Decision、Queue、保留與發布日常操作。

### 裝置與電子紙

- [`devices/ESP32_GUIDE_ZH_TW.md`](devices/ESP32_GUIDE_ZH_TW.md)：ESP32-S3、配網、Manifest、低功耗與燒錄。
- [`devices/ESP32_AUTOMATIC_PAIRING_ZH_TW.md`](devices/ESP32_AUTOMATIC_PAIRING_ZH_TW.md)：實體 possession 配對、pending enrollment、可恢復 claim／confirm、Device Secret、撤銷、repair 與 Stock 隔離。
- [`devices/DEVICE_COLOR_NOTIFICATION_GUIDE_ZH_TW.md`](devices/DEVICE_COLOR_NOTIFICATION_GUIDE_ZH_TW.md)：六／七色、抖動、ACK、通知與 Webhook。
- [`devices/DEVICE_PROTOCOL_ZH_TW.md`](devices/DEVICE_PROTOCOL_ZH_TW.md)：離線 Queue Manifest、下載與 ACK 協定。
- [`devices/DEVICE_TRANSPORT_SECURITY_ZH_TW.md`](devices/DEVICE_TRANSPORT_SECURITY_ZH_TW.md)：Device Secret／Legacy Token、HTTP/HTTPS 與傳輸限制。
- [`devices/ESP32_TLS_PROVISIONING_ZH_TW.md`](devices/ESP32_TLS_PROVISIONING_ZH_TW.md)：trust anchor、HTTPS fail-closed、私有 LAN 開發 HTTP 與隨機配網密碼。
- [`devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md`](devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md)：PhotoPainter 板型、電源、PSRAM 與實機驗收。
- [`devices/PHOTOPAINTER_DELIVERY_MODES_ZH_TW.md`](devices/PHOTOPAINTER_DELIVERY_MODES_ZH_TW.md)：Stock 相容、既有 Online、Enhanced 離線排程、`/dataUP` Payload 與安全邊界。
- [`devices/PHOTOPAINTER_RECOVERY_ZH_TW.md`](devices/PHOTOPAINTER_RECOVERY_ZH_TW.md)：PhotoPainter 交付恢復、停機條件與實機驗證邊界。
- [`devices/SECURE_OTA_DESIGN_ZH_TW.md`](devices/SECURE_OTA_DESIGN_ZH_TW.md)：尚未啟用 OTA 的安全前提。

### 決策與韌性

- [`resilience/DECISION_FEEDBACK_RESILIENCE_PLAN_ZH_TW.md`](resilience/DECISION_FEEDBACK_RESILIENCE_PLAN_ZH_TW.md)：Decision Trace、回饋、Queue、Retention、Canary 的整體設計。
- [`resilience/DECISION_TRACE_ZH_TW.md`](resilience/DECISION_TRACE_ZH_TW.md)：Trace 內容、查詢與回饋 API。
- [`resilience/SHADOW_MODE_ZH_TW.md`](resilience/SHADOW_MODE_ZH_TW.md)：不影響正式發布的 Shadow 比較模式。
- [`resilience/OFFLINE_QUEUE_ZH_TW.md`](resilience/OFFLINE_QUEUE_ZH_TW.md)：裝置離線內容佇列。
- [`resilience/DEVICE_QUEUE_AND_ROLLOUT_ZH_TW.md`](resilience/DEVICE_QUEUE_AND_ROLLOUT_ZH_TW.md)：Queue、LKG 與 Canary 邊界。
- [`resilience/CANARY_ROLLOUT_ZH_TW.md`](resilience/CANARY_ROLLOUT_ZH_TW.md)：Canary 發布與回滾行為。
- [`resilience/DATA_RETENTION_ZH_TW.md`](resilience/DATA_RETENTION_ZH_TW.md)：資料保留與 Dry Run 清理。

### 參考資料與歷史紀錄

- [`reference/TOKEN_COST_GUIDE_ZH_TW.md`](reference/TOKEN_COST_GUIDE_ZH_TW.md)：Token、預篩選、預算與成本邊界。
- [`reference/CHANGELOG.md`](reference/CHANGELOG.md)：版本變更記錄。
- [`archive/baselines/ARCHITECTURE_CURRENT.md`](archive/baselines/ARCHITECTURE_CURRENT.md)：2026-07-17 前的舊架構基線，非目前架構。
- [`archive/baselines/PROJECT_AUDIT_ZH_TW.md`](archive/baselines/PROJECT_AUDIT_ZH_TW.md)：重構前工程稽核，非目前行為。
- [`archive/audits/CONTROL_CENTER_AUDIT.md`](archive/audits/CONTROL_CENTER_AUDIT.md)：設定控制中心歷史稽核。
- [`archive/audits/LEGACY_MEMORY_DATA_SAFETY_AUDIT.md`](archive/audits/LEGACY_MEMORY_DATA_SAFETY_AUDIT.md)：Legacy 記憶體與資料安全稽核。
- [`archive/audits/SQLITE_AND_SCANNER_AUDIT.md`](archive/audits/SQLITE_AND_SCANNER_AUDIT.md)：SQLite／掃描器修正前稽核。
- [`archive/audits/OPENAI_BATCH_LIFECYCLE_AUDIT_ZH_TW.md`](archive/audits/OPENAI_BATCH_LIFECYCLE_AUDIT_ZH_TW.md)：OpenAI Batch 生命週期歷史稽核。
- [`archive/reports/FINAL_IMPLEMENTATION_REPORT_ZH_TW.md`](archive/reports/FINAL_IMPLEMENTATION_REPORT_ZH_TW.md)：2026-07-17 實作與驗收報告。
- [`archive/reports/FINAL_CROSS_MODULE_HARDENING_REVIEW_ZH_TW.md`](archive/reports/FINAL_CROSS_MODULE_HARDENING_REVIEW_ZH_TW.md)：2026-07-22 跨模組與硬體邊界報告。
- [`archive/reports/N100_IMPLEMENTATION_REPORT_ZH_TW.md`](archive/reports/N100_IMPLEMENTATION_REPORT_ZH_TW.md)：2026-07-18 N100 實作／量測報告。
- [`reports/PERFORMANCE_REPORT.md`](reports/PERFORMANCE_REPORT.md)：100,000 筆效能測試紀錄；測試腳本會更新此檔。
- [`reports/FINAL_ONE_SHOT_HARDENING_AUDIT.md`](reports/FINAL_ONE_SHOT_HARDENING_AUDIT.md)：本輪 P1、Provider、AI、Release、Container、文件與 hosted CI 交接證據。

### 元件旁文件

- [`../esp32/ink-display-133C-photo/README.md`](../esp32/ink-display-133C-photo/README.md)：133C 韌體元件說明。
- [`../esp32/ink-display-7C-photo/THIRD_PARTY_NOTICES.md`](../esp32/ink-display-7C-photo/THIRD_PARTY_NOTICES.md)：7C／PhotoPainter 韌體第三方授權聲明。
- [`../inktime/app/domain/rendering/font_assets/README.md`](../inktime/app/domain/rendering/font_assets/README.md)：內建繁體中文字型資產說明。
- [`../simulation_photos/README.md`](../simulation_photos/README.md)：模擬器照片投放資料夾說明。

## 維護這份索引

新增、移動或刪除任何 `.md` 時，必須同步更新本頁與根目錄 `USER_MANUAL.html` 的「完整文件索引」，並重新檢查 Markdown／HTML 相對連結。
