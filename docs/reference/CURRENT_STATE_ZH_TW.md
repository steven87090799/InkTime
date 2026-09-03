# InkTime 現行版本與功能基線

核對日期：2026-09-03。功能基準：`origin/main` 的 `51309e2`，加上本次保留退休設定的快照修復；文件與本機重建不表示 GitHub PR 已合併。部署中的版本請另從「診斷」核對 Git revision。

## 版本不是同一個數字

| 項目 | 原始碼值 | 權威來源 |
|---|---|---|
| Python 套件 | `2.0.0.dev0`，Python ≥3.10 | [`inktime/_version.py`](../../inktime/_version.py)、[`pyproject.toml`](../../pyproject.toml) |
| SQLite Migration | 連續 `1–57` | [`migrations.py`](../../inktime/app/db/migrations.py) |
| AI Analysis Schema | 嚴格 v4；舊 v1–v3 保留歷史，不參與 v4 排名 | [`plan.py`](../../inktime/app/domain/analysis/plan.py)、[`schema.py`](../../inktime/app/domain/analysis/schema.py) |
| ESP32 7C／PhotoPainter 韌體 | `2.8.6` | [`ink-display-7C-photo.ino`](../../esp32/ink-display-7C-photo/ink-display-7C-photo.ino) |
| ESP32 Config Store payload | v5，讀取 v1–v5；舊容量 12、新容量 24 slots | [`device_config_store_core.h`](../../esp32/ink-display-7C-photo/device_config_store_core.h) |
| 設定匯出格式 | v1 | [`settings.py`](../../inktime/app/repositories/settings.py) |
| NAS deployment contract | `3` | [`nas-deployment-contract.version`](../../nas-deployment-contract.version) |

Config Store v5 是裝置本機儲存格式，不是所有 HTTP Manifest 的版本。Release、Queue 與 offline schedule 各自有協定欄位，不能一律改成 v5。

## 執行與分析

- 三程序為 Web（`server:app`）、Worker（`inktime.app.workers.runner`）、Scheduler（`inktime.app.workers.scheduler`），共用 `/data` 與唯讀 `/photos`。
- 新安裝 `analysis.execution_mode=local_only`。只有設定 Provider 不會開啟 AI；一般 AI 工作需要 `automatic_ai`，單張手動 AI 可使用 `local_with_manual_ai`。`disabled` 拒絕新分析，但既有照片與 Release 仍可讀。
- 新策略為 `local`／`single`；舊的 `low_cost`、`smart`、`smart_two_stage`、`high_quality`、`single_high`、`custom` 正規化成 `single`，不恢復兩階段圖片分析。一次分析計畫最多一次圖片 Vision，必要時最多一次純文字 JSON 修復；另建重跑工作仍可能產生新費用。
- Web 的 `analysis.image_max_side` 預設 1024、可選 1600；底層 plan／benchmark 額外支援 512。不要把低解析度誤寫成第一階段。
- Migration 51 增加有界 AI Trace；Migration 52 增加 `providers.model`。Provider 專屬模型優先於全域模型，留白才沿用；OpenRouter 必須使用完整模型 ID。
- Schema v4 的回憶／視覺／本機品質固定為 50／25／25，加上本機特殊程度與照片庫稀有度；語意與本機品質分開排名。E6 只參與顯示分數；內容分類有獨立門檻與人工恢復保護，見 [Vision v4](../VISION_SCHEMA_V4.md)。
- 照片庫優先顯示現行 v4 模型，再顯示已保存的歷史模型紀錄及本機分析；歷史描述／短句可搜尋、原始評分可查閱，仍不參與 v4 排名。儀表板分開標示含本機的完成狀態與依照片去重的模型結果。
- `completed` 只表示工作結束；本機、預篩排除、繼承或 cache hit 不證明有新 API 請求。請合併工作策略、AI Trace attempts、`api_usage` 與時間戳判讀。

操作見[管理員指南](../guides/ADMIN_GUIDE_ZH_TW.md)、[本機選片](../guides/LOCAL_ONLY_SELECTION_ZH_TW.md)與[Activity／AI Trace](../guides/ACTIVITY_AI_TRACE_ZH_TW.md)。設定頁完整列出設定，提供全文、分類、風險與生效方式篩選；功能與設定說明大全位於 `/help/controls`。

## 渲染與裝置

- 8 種版型：`full`、`postcard`、`photo_info`、`photo_pair`、`photo_pair_caption`、`adaptive_memory`、`calendar`、`weather_sensor`；後兩者限直向。
- 3 種 480×800 Profile：`safe_4c`（2bpp，96,000 bytes）、`gdep073e01_6c`、`gdey073d46_7c`（indexed4，192,000 bytes）。PhotoPainter 在裝置端轉為原生 800×480。
- 10 個抖動選項（含別名／無抖動）：`none`、`floyd_steinberg`、`gooddisplay`、`photo_smooth`、`atkinson`、`bayer4`、`bayer8`、`nearest`、`bayer_ordered`、`serpentine_floyd_steinberg`。
- 新安裝渲染預設為 `gdep073e01_6c`、`gooddisplay`、`photo_info`、`portrait`、`stretch_fill`；新增裝置的預設 Profile 同為 `gdep073e01_6c`，仍必須另外與實板匹配。
- Enhanced offline schedule 依配對確認的能力允許 12／24 slots；能力不明的舊裝置需 repair／重新配對，不直接推定為 24。
- 2.8.6 PhotoPainter KEY1 雙擊顯示唯讀電源頁，顯示後停留 30 秒，再驗證 SD 最後成功 frame 並恢復原圖；無有效 frame 才回正常網路流程。GPIO0 BOOT、GPIO5 PWR、GPIO21 TG28 IRQ 保持安全邊界。
- 13.3 吋程式是保留的 beta 舊協定實作；目前 Web 沒有相應正式 Profile。不能直接使用本版 7.3 吋 Release。

## 部署、保存與驗收

NAS 使用[Tag 更新器](../operations/NAS_TAG_DEPLOYMENT_ZH_TW.md)拉取已發布映像，驗證 marker、lock、部署契約與 recovery point 後才重建。開發 Compose 與正式 NAS 流程分開；一般開發驗證遵守 [`AGENTS.md`](../../AGENTS.md)／[CI policy](../CI_POLICY.md)。

一般 Web 備份預設排除 Secrets、原圖與 Release payload；NAS update recovery point 另保存含 Secrets 的 DB 與受保護的 session key。這些檔案不得公開。AI Trace 預設保留 30 天；API usage 原始預設為 400 天，管理員已改過的政策不應被覆蓋。Photo Analysis 歷史清理另有 dry-run digest 與明確確認，詳見[保留指南](../operations/PHOTO_ANALYSIS_RETENTION_ZH_TW.md)。

本次另依部署者授權重建 OrbStack debug 三服務，驗證 Migration 53→57、ready／login 與帳號、Provider、模型價格、Secrets、Session Key 和個人設定保留；這是本機環境證據。pytest 與完整回歸仍由目前提交的 Hosted CI 決定；付費 API、NAS 更新及刷機未執行。歷史 CI 與量測保存原日期。PhotoPainter 2026-08-22／23 的局部實板結果仍見[硬體交接](../devices/PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md)，不推廣為目前全部功能已驗收。
