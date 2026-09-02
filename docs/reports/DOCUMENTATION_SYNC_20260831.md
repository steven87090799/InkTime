# 2026-08-31 全專案 Markdown 校對紀錄

> 歷史校對紀錄，以下數量與版本保留當時結果；2026-09-03 現況以[現行基線](../reference/CURRENT_STATE_ZH_TW.md)為準。

原始碼基準：`origin/main` `48d2b8d`。工作分支：`docs/project-documentation-sync-20260831`。本次只調整文件；未合併的其他 worktree 功能不列入主線，也不代表使用者部署已更新。

範圍為原有 75 份 Markdown，加上本次新增 4 份，共 79 份；依既有分類校對，不搬移文件。根目錄 `USER_MANUAL.html` 同步完整索引、現行基線提醒及少量衝突文字，未把既有長篇 HTML 宣稱為逐項更新的現行規格。

## 主要修正

- README／安裝：三程序、NAS 拉取已發布 Tag、開發 Compose 與原生環境變數分開；移除 ExifTool、舊 analyzer／renderer 啟動依賴。
- AI：local-only 預設、single／JSON repair、Provider 專屬模型、OpenRouter 完整 ID、Activity／AI Trace 與成本證據。
- 版本／維運：Migration 52、Schema v3、備份類型、分析歷史與 Trace／usage 保留範圍。
- 渲染／裝置：8 版型、3 Profile、10 抖動選項、韌體 2.8.6、Config Store v5、12／24 slots、KEY1 恢復照片及 PhotoPainter 安全邊界。
- 歷史：保留舊 PR、量測與實板結果，只補日期／範圍或修連結；AGENTS 與第三方授權原文不改。

## 靜態檢查與驗收界線

交付前靜態檢查結果：

| 檢查 | 結果 |
|---|---|
| Markdown 文件／兩份完整索引 | 79 份；`docs/README.md` 與 `USER_MANUAL.html` 各涵蓋 79／79 |
| 本地相對連結與頁內錨點 | 635 處連結、124 個錨點，未發現失效項目；未檢查外站即時可用性 |
| Markdown fenced code blocks | 全部閉合 |
| HTML inline JavaScript | 2 個 script 通過 `node --check` 靜態語法檢查；未做瀏覽器驗收 |
| 管理員表格預設值 | 53 個可由 AST 直接讀取的 literal 與原始碼一致，其餘敘述由來源核對 |
| Migration 與字型資產 | AST 註冊連續 1–52；兩份 TTF 的 SHA-256 符合元件 README |
| 差異範圍／空白檢查 | 僅 Markdown 與 HTML 文件；`git diff --check` 通過 |
| 原工作區 | 原 HEAD `9fa93dc`；修改及未追蹤檔案清單與開始時相同，未覆寫既有檔案 |
| 主線基準 | 交付前 `git ls-remote` 確認 main 仍為 `48d2b8d85bec27f68232baa4d38438d6a4b55209` |

原有 Markdown 修訂 61 份、保留 14 份，新增 4 份，另同步 HTML 索引。未執行 pytest、容器建置、瀏覽器／Runtime smoke、模型 API、NAS 更新、韌體編譯或實板操作；沒有本次 Hosted CI／部署／硬體 PASS。本次校對只修改文件；commit、遠端分支與 PR 狀態請以 Git／GitHub 為準，不能由此報告推定 CI 已通過或變更已合併。

## 逐檔範圍

「保留」表示已核對適用範圍、索引與連結，沒有需修改的現行契約，或原文屬歷史／授權／規則；不以改動時間製造更新。

| 文件 | 處理 | 校對依據／範圍 |
|---|---|---|
| [`AGENTS.md`](../../AGENTS.md) | 保留 | 保留原 Hosted CI、交付及硬體安全規則 |
| [`README.en.md`](../../README.en.md) | 修訂 | 現行版本／功能基線與全文件索引 |
| [`README.md`](../../README.md) | 修訂 | 現行版本／功能基線與全文件索引 |
| [`docs/CI_POLICY.md`](../CI_POLICY.md) | 修訂 | AGENTS 與 GitHub workflows；不變更交付規則 |
| [`docs/OPENAI_BATCH_ANALYSIS_ZH_TW.md`](../OPENAI_BATCH_ANALYSIS_ZH_TW.md) | 修訂 | Batch lifecycle／privacy／usage 邊界與執行模式 |
| [`docs/PRODUCTION_READINESS_SECURITY_HANDOFF_ZH_TW.md`](../PRODUCTION_READINESS_SECURITY_HANDOFF_ZH_TW.md) | 修訂 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/README.md`](../README.md) | 修訂 | 現行版本／功能基線與全文件索引 |
| [`docs/architecture/APPLICATION_FACTORY.md`](../architecture/APPLICATION_FACTORY.md) | 修訂 | Factory、RuntimeConfig、分析／渲染領域與現行服務分層 |
| [`docs/architecture/ARCHITECTURE_TARGET.md`](../architecture/ARCHITECTURE_TARGET.md) | 修訂 | Factory、RuntimeConfig、分析／渲染領域與現行服務分層 |
| [`docs/architecture/ARCHITECTURE_ZH_TW.md`](../architecture/ARCHITECTURE_ZH_TW.md) | 修訂 | Factory、RuntimeConfig、分析／渲染領域與現行服務分層 |
| [`docs/architecture/LEGACY_RETIREMENT_PLAN.md`](../architecture/LEGACY_RETIREMENT_PLAN.md) | 保留 | Factory、RuntimeConfig、分析／渲染領域與現行服務分層 |
| [`docs/architecture/RUNTIME_CONFIGURATION.md`](../architecture/RUNTIME_CONFIGURATION.md) | 修訂 | Factory、RuntimeConfig、分析／渲染領域與現行服務分層 |
| [`docs/architecture/TECH_DEBT_LOCAL_ONLY_ZH_TW.md`](../architecture/TECH_DEBT_LOCAL_ONLY_ZH_TW.md) | 修訂 | Factory、RuntimeConfig、分析／渲染領域與現行服務分層 |
| [`docs/architecture/VISUAL_ORIENTATION_CORRECTION.md`](../architecture/VISUAL_ORIENTATION_CORRECTION.md) | 修訂 | Factory、RuntimeConfig、分析／渲染領域與現行服務分層 |
| [`docs/archive/audits/CONTROL_CENTER_AUDIT.md`](../archive/audits/CONTROL_CENTER_AUDIT.md) | 修訂 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/archive/audits/LEGACY_MEMORY_DATA_SAFETY_AUDIT.md`](../archive/audits/LEGACY_MEMORY_DATA_SAFETY_AUDIT.md) | 保留 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/archive/audits/OPENAI_BATCH_LIFECYCLE_AUDIT_ZH_TW.md`](../archive/audits/OPENAI_BATCH_LIFECYCLE_AUDIT_ZH_TW.md) | 修訂 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/archive/audits/SQLITE_AND_SCANNER_AUDIT.md`](../archive/audits/SQLITE_AND_SCANNER_AUDIT.md) | 保留 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/archive/baselines/ARCHITECTURE_CURRENT.md`](../archive/baselines/ARCHITECTURE_CURRENT.md) | 保留 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/archive/baselines/PROJECT_AUDIT_ZH_TW.md`](../archive/baselines/PROJECT_AUDIT_ZH_TW.md) | 保留 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/archive/reports/FINAL_CROSS_MODULE_HARDENING_REVIEW_ZH_TW.md`](../archive/reports/FINAL_CROSS_MODULE_HARDENING_REVIEW_ZH_TW.md) | 保留 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/archive/reports/FINAL_IMPLEMENTATION_REPORT_ZH_TW.md`](../archive/reports/FINAL_IMPLEMENTATION_REPORT_ZH_TW.md) | 修訂 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/archive/reports/FINAL_ONE_SHOT_HARDENING_AUDIT.md`](../archive/reports/FINAL_ONE_SHOT_HARDENING_AUDIT.md) | 修訂 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/archive/reports/N100_IMPLEMENTATION_REPORT_ZH_TW.md`](../archive/reports/N100_IMPLEMENTATION_REPORT_ZH_TW.md) | 保留 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/archive/reports/branch-consolidation-report.md`](../archive/reports/branch-consolidation-report.md) | 修訂 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/devices/DEVICE_COLOR_NOTIFICATION_GUIDE_ZH_TW.md`](../devices/DEVICE_COLOR_NOTIFICATION_GUIDE_ZH_TW.md) | 修訂 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/devices/DEVICE_PROTOCOL_ZH_TW.md`](../devices/DEVICE_PROTOCOL_ZH_TW.md) | 修訂 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/devices/DEVICE_TRANSPORT_SECURITY_ZH_TW.md`](../devices/DEVICE_TRANSPORT_SECURITY_ZH_TW.md) | 保留 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/devices/ESP32_AUTOMATIC_PAIRING_ZH_TW.md`](../devices/ESP32_AUTOMATIC_PAIRING_ZH_TW.md) | 修訂 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/devices/ESP32_GUIDE_ZH_TW.md`](../devices/ESP32_GUIDE_ZH_TW.md) | 修訂 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/devices/ESP32_TLS_PROVISIONING_ZH_TW.md`](../devices/ESP32_TLS_PROVISIONING_ZH_TW.md) | 修訂 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/devices/PHOTOPAINTER_DELIVERY_MODES_ZH_TW.md`](../devices/PHOTOPAINTER_DELIVERY_MODES_ZH_TW.md) | 修訂 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/devices/PHOTOPAINTER_RECOVERY_ZH_TW.md`](../devices/PHOTOPAINTER_RECOVERY_ZH_TW.md) | 修訂 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/devices/PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md`](../devices/PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md) | 修訂 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/devices/SECURE_OTA_DESIGN_ZH_TW.md`](../devices/SECURE_OTA_DESIGN_ZH_TW.md) | 修訂 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md`](../devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md) | 修訂 | 7C 韌體、hardware_profile、Config Store、裝置 API／Repository；保留既有實板證據 |
| [`docs/getting-started/DEVELOPMENT_GUIDE_ZH_TW.md`](../getting-started/DEVELOPMENT_GUIDE_ZH_TW.md) | 修訂 | 三程序入口、Compose／環境變數、Settings、AGENTS |
| [`docs/getting-started/INSTALLATION_ZH_TW.md`](../getting-started/INSTALLATION_ZH_TW.md) | 修訂 | 三程序入口、Compose／環境變數、Settings、AGENTS |
| [`docs/getting-started/QUICK_START_ZH_TW.md`](../getting-started/QUICK_START_ZH_TW.md) | 修訂 | 三程序入口、Compose／環境變數、Settings、AGENTS |
| [`docs/guides/ACTIVITY_AI_TRACE_ZH_TW.md`](../guides/ACTIVITY_AI_TRACE_ZH_TW.md) | 新增 | Web templates／API、Settings、Jobs／Worker、Provider 與選片實作 |
| [`docs/guides/ADMIN_GUIDE_ZH_TW.md`](../guides/ADMIN_GUIDE_ZH_TW.md) | 修訂 | Web templates／API、Settings、Jobs／Worker、Provider 與選片實作 |
| [`docs/guides/API_PROVIDER_GUIDE_ZH_TW.md`](../guides/API_PROVIDER_GUIDE_ZH_TW.md) | 修訂 | Web templates／API、Settings、Jobs／Worker、Provider 與選片實作 |
| [`docs/guides/EPAPER_SIMULATOR_ZH_TW.md`](../guides/EPAPER_SIMULATOR_ZH_TW.md) | 修訂 | Web templates／API、Settings、Jobs／Worker、Provider 與選片實作 |
| [`docs/guides/LOCAL_ONLY_SELECTION_ZH_TW.md`](../guides/LOCAL_ONLY_SELECTION_ZH_TW.md) | 修訂 | Web templates／API、Settings、Jobs／Worker、Provider 與選片實作 |
| [`docs/guides/USER_GUIDE_ZH_TW.md`](../guides/USER_GUIDE_ZH_TW.md) | 修訂 | Web templates／API、Settings、Jobs／Worker、Provider 與選片實作 |
| [`docs/operations/BACKUP_RESTORE_ZH_TW.md`](../operations/BACKUP_RESTORE_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/DOCKER_GUIDE_ZH_TW.md`](../operations/DOCKER_GUIDE_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/ERROR_CODES_ZH_TW.md`](../operations/ERROR_CODES_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/LOGGING_GUIDE_ZH_TW.md`](../operations/LOGGING_GUIDE_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/MIGRATION_GUIDE_ZH_TW.md`](../operations/MIGRATION_GUIDE_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/MIGRATION_PLAN.md`](../operations/MIGRATION_PLAN.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/N100_RESOURCE_GUIDE_ZH_TW.md`](../operations/N100_RESOURCE_GUIDE_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/NAS_TAG_DEPLOYMENT_ZH_TW.md`](../operations/NAS_TAG_DEPLOYMENT_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/OPERATIONS_ZH_TW.md`](../operations/OPERATIONS_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/PHOTO_ANALYSIS_RETENTION_ZH_TW.md`](../operations/PHOTO_ANALYSIS_RETENTION_ZH_TW.md) | 保留 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/PRODUCTION_PREFLIGHT_ZH_TW.md`](../operations/PRODUCTION_PREFLIGHT_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/SECRET_RECOVERY_ZH_TW.md`](../operations/SECRET_RECOVERY_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/SECURITY_GUIDE_ZH_TW.md`](../operations/SECURITY_GUIDE_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/operations/TROUBLESHOOTING_ZH_TW.md`](../operations/TROUBLESHOOTING_ZH_TW.md) | 修訂 | Compose、NAS updater、migration、backup、healthcheck、maintenance 實作 |
| [`docs/post-merge-hardware-validation.md`](../post-merge-hardware-validation.md) | 修訂 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/providers/MODEL_BENCHMARK_ZH_TW.md`](../providers/MODEL_BENCHMARK_ZH_TW.md) | 修訂 | OpenRouter client／validator、Analysis Plan、benchmark CLI；不重做模型實測 |
| [`docs/providers/OPENROUTER_ZH_TW.md`](../providers/OPENROUTER_ZH_TW.md) | 修訂 | OpenRouter client／validator、Analysis Plan、benchmark CLI；不重做模型實測 |
| [`docs/reference/CHANGELOG.md`](../reference/CHANGELOG.md) | 修訂 | 版本宣告、migrations、Analysis Plan、Provider／usage 與 render options |
| [`docs/reference/CURRENT_STATE_ZH_TW.md`](../reference/CURRENT_STATE_ZH_TW.md) | 新增 | 版本宣告、migrations、Analysis Plan、Provider／usage 與 render options |
| [`docs/reference/TOKEN_COST_GUIDE_ZH_TW.md`](../reference/TOKEN_COST_GUIDE_ZH_TW.md) | 修訂 | 版本宣告、migrations、Analysis Plan、Provider／usage 與 render options |
| [`docs/reports/DOCUMENTATION_SYNC_20260831.md`](DOCUMENTATION_SYNC_20260831.md) | 新增 | 本次校對範圍與靜態檢查紀錄 |
| [`docs/reports/PERFORMANCE_REPORT.md`](PERFORMANCE_REPORT.md) | 修訂 | 歷史原文與既有驗收日期／版本；不改成目前 PASS |
| [`docs/resilience/CANARY_ROLLOUT_ZH_TW.md`](../resilience/CANARY_ROLLOUT_ZH_TW.md) | 修訂 | Decision／Queue／Canary／Retention services 與 Repository |
| [`docs/resilience/DATA_RETENTION_ZH_TW.md`](../resilience/DATA_RETENTION_ZH_TW.md) | 修訂 | Decision／Queue／Canary／Retention services 與 Repository |
| [`docs/resilience/DECISION_FEEDBACK_RESILIENCE_PLAN_ZH_TW.md`](../resilience/DECISION_FEEDBACK_RESILIENCE_PLAN_ZH_TW.md) | 修訂 | Decision／Queue／Canary／Retention services 與 Repository |
| [`docs/resilience/DECISION_TRACE_ZH_TW.md`](../resilience/DECISION_TRACE_ZH_TW.md) | 修訂 | Decision／Queue／Canary／Retention services 與 Repository |
| [`docs/resilience/DEVICE_QUEUE_AND_ROLLOUT_ZH_TW.md`](../resilience/DEVICE_QUEUE_AND_ROLLOUT_ZH_TW.md) | 保留 | Decision／Queue／Canary／Retention services 與 Repository |
| [`docs/resilience/OFFLINE_QUEUE_ZH_TW.md`](../resilience/OFFLINE_QUEUE_ZH_TW.md) | 保留 | Decision／Queue／Canary／Retention services 與 Repository |
| [`docs/resilience/SHADOW_MODE_ZH_TW.md`](../resilience/SHADOW_MODE_ZH_TW.md) | 保留 | Decision／Queue／Canary／Retention services 與 Repository |
| [`esp32/ink-display-133C-photo/README.md`](../../esp32/ink-display-133C-photo/README.md) | 修訂 | 對應 sketch、hardware profile／安全界線 |
| [`esp32/ink-display-7C-photo/README.md`](../../esp32/ink-display-7C-photo/README.md) | 新增 | 對應 sketch、hardware profile／安全界線 |
| [`esp32/ink-display-7C-photo/THIRD_PARTY_NOTICES.md`](../../esp32/ink-display-7C-photo/THIRD_PARTY_NOTICES.md) | 保留 | 保留授權聲明原文；索引／連結核對 |
| [`inktime/app/domain/rendering/font_assets/README.md`](../../inktime/app/domain/rendering/font_assets/README.md) | 修訂 | 本機字型檔案與字型載入／授權說明 |
| [`simulation_photos/README.md`](../../simulation_photos/README.md) | 修訂 | 模擬器照片目錄與明確掃描規則 |
