# InkTime 現行版本與功能基線

本分支更新日期：2026-10-17；基底為 `origin/main=80f3121`，NAS 可靠性修復見 PR #162。下表 PhotoPainter `2.8.9` 包含完整 HTTP JSON 接收修復，仍須以該提交的 Hosted CI 結果確認編譯；原碼版本不表示已部署或刷機。部署中的版本請從「診斷」核對 Git revision，實體裝置則核對回報的韌體版本。

## 版本不是同一個數字

| 項目 | 原始碼值 | 權威來源 |
|---|---|---|
| Python 套件 | `2.0.0.dev0`，部署／CI 支援 Python 3.12（`>=3.12,<3.13`） | [`inktime/_version.py`](../../inktime/_version.py)、[`pyproject.toml`](../../pyproject.toml) |
| SQLite Migration | 連續 `1–71`；64–66 保護配送／渲染輸入並支援雙圖附說明，67 索引活動清理，68 永久保存照片內容版本與模型證據，69 加入 Batch 候選分頁索引與逐項失敗重試授權，70 支援自動版型，71 將未經管理員另選的舊預設色彩升為 D；保留歷史／外鍵 | [`migrations.py`](../../inktime/app/db/migrations.py) |
| AI Analysis Schema | 嚴格 v5；保留 v4 相容閱讀與既有結果，不為精簡欄位重新付費 | [`plan.py`](../../inktime/app/domain/analysis/plan.py)、[`schema.py`](../../inktime/app/domain/analysis/schema.py) |
| ESP32 7C／PhotoPainter 韌體 | `2.8.9`（本分支；未刷機） | [`ink-display-7C-photo.ino`](../../esp32/ink-display-7C-photo/ink-display-7C-photo.ino) |
| ESP32 Config Store payload | v5，讀取 v1–v5；舊容量 12、新 payload 24 slots read compatibility | [`device_config_store_core.h`](../../esp32/ink-display-7C-photo/device_config_store_core.h) |
| PhotoPainter Enhanced storage | Internal FFat；40 formal frames hard limit；16 offline slots/day capability；microSD not required | [`photopainter_support.cpp`](../../esp32/ink-display-7C-photo/photopainter_support.cpp)、[partition table](../../esp32/ink-display-7C-photo/inktime_photopainter_3M_16MB.csv) |
| 設定匯出格式 | v1 | [`settings.py`](../../inktime/app/repositories/settings.py) |
| NAS deployment contract | `3` | [`nas-deployment-contract.version`](../../nas-deployment-contract.version) |

Config Store v5 是裝置本機儲存格式，不是所有 HTTP Manifest 的版本。Release、Queue 與 offline schedule 各自有協定欄位，不能一律改成 v5。

## 執行與分析

- 三程序為 Web（`server:app`）、Worker（`inktime.app.workers.runner`）、Scheduler（`inktime.app.workers.scheduler`），共用 `/data` 與唯讀 `/photos`。
- 新安裝 `analysis.execution_mode=local_only`。只有設定 Provider 不會開啟 AI；一般 AI 工作需要 `automatic_ai`，單張手動 AI 可使用 `local_with_manual_ai`。`disabled` 拒絕新分析，但既有照片與 Release 仍可讀。
- 新策略為 `local`／`single`；舊的 `low_cost`、`smart`、`smart_two_stage`、`high_quality`、`single_high`、`custom` 正規化成 `single`，不恢復兩階段圖片分析。一次分析計畫最多一次圖片 Vision，只在本機抽取 JSON 並嚴格驗證，不追加模型 JSON 修復或長描述請求；另建重跑工作仍可能產生新費用。
- 新分析提示詞為 `photo-analysis-v5-complete-literary-v2-20261011`，逐字保留原版規則，再補充文青表達、分類信心、EXIF 與完整精簡 JSON 要求；舊原版、672 字元版及第一個完整版本的凍結工作各自使用原文字。實際請求與費用預覽共用最高 512 output tokens；圖片輸入 Token 依模型與像素計費，預覽只是情境而非保證上限。
- 同步分析的候選選取與保存共用指紋，顯示控制與停用的修復策略不影響是否已完成。定期分析先凍結 Provider／模型／提示詞／規則與執行模式；只有自動 AI 模式允許建立定期雲端分析工作。
- SQLite 連線採 WAL／`synchronous=FULL`／外鍵檢查，優先保留已提交的分析與費用紀錄；仍需本機可靠檔案系統、完整備份及還原驗證，不能據此宣稱硬體斷電驗收。
- Web 的 `analysis.image_max_side` 預設 1024、可選 1600；底層 plan／benchmark 額外支援 512。不要把低解析度誤寫成第一階段。
- Migration 51 增加有界 AI Trace；Migration 52 增加 `providers.model`。Provider 專屬模型優先於全域模型，留白才沿用；OpenRouter 必須使用完整模型 ID。
- 現行 v5 與相容 v4 採 `ranking-v5-ai-first`：回憶 67%、視覺 33%、本機品質 0%，再套用 special bonus 與最愛提升。本機品質現在是 candidate qualification／quality gate，不是 ranking weight；它只負責本機特徵完成、品質與來源資格判斷，不與 semantic 分數混比或補位。E6 只參與顯示分數；內容分類有獨立門檻與人工恢復保護，見 [現行 Vision Schema](../VISION_SCHEMA.md)。
- 照片庫優先顯示現行 v5／相容 v4 模型，再顯示已保存的歷史模型紀錄及本機分析；歷史描述／短句可搜尋、原始評分可查閱，v1–v3 歷史分數不參與現行 semantic 排名。儀表板分開標示含本機的完成狀態與依照片去重的模型結果。
- `completed` 只表示工作結束；本機、預篩排除、繼承或 cache hit 不證明有新 API 請求。請合併工作策略、AI Trace attempts、`api_usage` 與時間戳判讀。

操作見[管理員指南](../guides/ADMIN_GUIDE_ZH_TW.md)、[本機選片](../guides/LOCAL_ONLY_SELECTION_ZH_TW.md)與[Activity／AI Trace](../guides/ACTIVITY_AI_TRACE_ZH_TW.md)。設定頁完整列出設定，提供全文、分類、風險與生效方式篩選；功能與設定說明大全位於 `/help/controls`。

## 渲染與裝置

- 9 種版型：`auto_photo`、`full`、`postcard`、`photo_info`、`photo_pair`、`photo_pair_caption`、`adaptive_memory`、`calendar`、`weather_sensor`；後兩者限直向。
- 3 種 480×800 Profile：`safe_4c`（2bpp，96,000 bytes）、`gdep073e01_6c`、`gdey073d46_7c`（indexed4，192,000 bytes）。PhotoPainter 在裝置端轉為原生 800×480。
- 12 個抖動選項（含別名／無抖動）：`natural_d`、`warm_vivid_c`、`none`、`floyd_steinberg`、`gooddisplay`、`photo_smooth`、`atkinson`、`bayer4`、`bayer8`、`nearest`、`bayer_ordered`、`serpentine_floyd_steinberg`。
- 新安裝渲染預設為 `gdep073e01_6c`、`natural_d`、`auto_photo`、`landscape`、`cover`；新增裝置的預設 Profile 同為 `gdep073e01_6c`，仍必須另外與實板匹配。
- D 自然膚色是自訂配方，以 epdoptimize 1.3.0 的 balanced 為基礎：linear-sRGB 白平衡 gains `(1, 0.985, 1.16)` 並正規化中性亮度、HSL 飽和度微降 10%、LAB display 動態範圍、97 百分位亮部保白（最低 luma 200、最高飽和度 0.22）、RGB 配色與蛇形 Floyd–Steinberg。六色面板使用 upstream 的 aitjcize appearance 色盤，輸出維持原有裝置色碼；亮部保白可能犧牲細微明暗。原版與舊 C 微暖增豔仍可切換。
- D 調色以最多 2048 像素的分塊運算，輸入上限為一張 480×800 已排版圖片；不載入整個照片庫、不保留影像或 diffusion table 的全域快取，也不新增 Node 常駐程序或執行期套件。每張圖的工作緩衝在該次呼叫結束後釋放；NAS 實際 RSS／CPU 峰值仍需實機量測。文字遮罩保持黑白；預覽與 Release 共用 `encode_image`，Release 保存配方版本與色盤。
- Migration 71 僅轉換 `render.dither=warm_vivid_c` 且沒有 `updated_by` 的舊預設；保留管理員選項、既有 Release 與快取排程內容。更新後的新預覽與新出圖才使用 D，不強制重繪或重送已快取的照片。
- Enhanced offline schedule 的 PhotoPainter 2.8.7 以後正式 capability 上限為 16 slots/day；Config Store v5 仍可讀 24-slot payload，能力不明的舊裝置需 repair／重新配對，不直接推定為 16。
- 2.8.7 以後 PhotoPainter KEY1 雙擊顯示唯讀電源頁，顯示後停留 30 秒，再驗證 Internal Flash 的正式 frame 並恢復原圖；無有效 frame 才回正常網路流程。GPIO0 BOOT、GPIO5 PWR、GPIO21 TG28 IRQ 保持安全邊界。
- 13.3 吋程式是保留的 beta 舊協定實作；目前 Web 沒有相應正式 Profile。不能直接使用本版 7.3 吋 Release。

## 部署、保存與驗收

NAS 使用[Tag 更新器](../operations/NAS_TAG_DEPLOYMENT_ZH_TW.md)拉取已發布映像，驗證 marker、lock、部署契約與 recovery point 後才重建。開發 Compose 與正式 NAS 流程分開；一般開發驗證遵守 [`AGENTS.md`](../../AGENTS.md)／[CI policy](../CI_POLICY.md)。

一般 Web 備份預設排除 Secrets、原圖與 Release payload；NAS update recovery point 另保存含 Secrets 的 DB 與受保護的 session key。這些檔案不得公開。AI Trace 預設保留 30 天；API usage 原始預設為 400 天，管理員已改過的政策不應被覆蓋。Photo Analysis 歷史清理另有 dry-run digest 與明確確認，詳見[保留指南](../operations/PHOTO_ANALYSIS_RETENTION_ZH_TW.md)。

歷史紀錄（2026-09-06）：當時依部署者授權重建 OrbStack debug 三服務，驗證 Migration 53→58、ready／login 與帳號、Provider、模型價格、Secrets、Session Key 和個人設定保留；這是本機環境證據。pytest 與完整回歸仍由目前提交的 Hosted CI 決定；付費 API、NAS 更新及刷機未執行。歷史 CI 與量測保存原日期。PhotoPainter 2026-08-22／23 的局部實板結果仍見[硬體交接](../devices/PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md)，不推廣為目前全部功能已驗收。

本次文件整理未重新部署、呼叫付費 API 或執行實板驗收；本機文件檢查與推送後 Hosted CI 證據分開記錄。Migration 58 更新 AI-first 衍生排名；Migration 59 新增已確認的 16-slot PhotoPainter 能力；Migration 60 將歷史直接加分欄位明確改名，現行 Favorite 固定只提升 special level。AI agent 規則以 [AGENTS.md](../../AGENTS.md) 為唯一入口；未知位置才使用 [AI 修改導航](../AI_NAVIGATION.md)。

Migration 61 加入付費操作檢查點、共享預算／限流及費用封存；修復範圍與尚待驗證項目見 [審查修復紀錄](../operations/REVIEW_REMEDIATION_20260916.md)。

Migration 62 加入 `runtime_identity` 密鑰指紋，並啟用五種未經管理員修改的預設清理政策；Migration 63 定點修復分析 `types` enum 並保留 Shadow 觀察模式。詳見[升級說明](../operations/MIGRATION_GUIDE_ZH_TW.md)與[資料保留](../resilience/DATA_RETENTION_ZH_TW.md)。已有指紋的資料庫若與 `session.key` 不符會以 `SESSION-003` 停止啟動；沒有舊指紋的歷史備份不能據此證明密鑰正確。

未對帳的付費請求與預算保留額不會因經過 24 小時而自動釋放。只有未送出或已有完整費用證據才能回收；缺少 usage 不等於明確零費用。原始碼入口：`repositories/usage.py`、`services/budgets.py`、`workers/scheduler.py`；修復背景見[2026-09-21 紀錄](../operations/CODE_REVIEW_FOLLOWUP_20260921.md)。

Migration 68 保存同一路徑內容變更前的照片與分析快照，並以付費操作 ID 配對完整模型 request／response；實際送出影像以內容雜湊放在 `/data/model-inputs`。預設不自動清除 AI Trace，Batch 本機輸入／輸出預設永久保留（`batch.local_retention_days=0`）。完整重裝／備份步驟與必要、建議、可調整設定見[首次使用 Checklist](../getting-started/FIRST_USE_CHECKLIST_ZH_TW.md)。一般送出會沿用已完成的付費 checkpoint，快取遺失不授權再次付費；所有已排除照片均須先明確恢復。
