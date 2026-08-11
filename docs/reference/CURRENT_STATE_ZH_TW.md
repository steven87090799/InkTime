# InkTime 目前實作狀態

本頁是現行文件的版本與架構基準；內容直接對照目前程式碼、設定、Migration、韌體與 CI 契約。歷史稽核與報告保留當時證據，若與本頁不同，以目前來源碼與本頁為準。

## 版本與 Schema

| 契約 | 目前值 | 權威來源 |
|---|---:|---|
| Python 套件版本 | `2.0.0.dev0` | `inktime/_version.py` |
| Python 支援 | 3.10+；正式映像使用 3.12 | `pyproject.toml`、`Dockerfile` |
| SQLite Database Schema | Migration `50` | `inktime/app/db/migrations.py` |
| Analysis Schema | `3`；相容讀取舊 v1／v2 | `inktime/app/domain/analysis/plan.py`、`schema.py` |
| Settings 匯出格式 | `1` | `inktime/app/repositories/settings.py` |
| Release Manifest | 2bpp 為 v1；indexed4 為 v2 | `inktime/app/domain/rendering/release.py` |
| Queue／Offline Schedule Manifest | `1` | `device_queue_manifests.py`、`api/devices.py` |
| ESP32 韌體 | `2.8.0` | `esp32/ink-display-7C-photo/ink-display-7C-photo.ino` |
| ESP32 Config Store | 寫入 v5；相容讀取 v1–v5；v1–v4 固定為 legacy 12-slot | `device_config_store_core.h` |
| Server device config | 一般裝置 v2；Enhanced 離線裝置 v3 | `inktime/app/api/devices.py` |

Migration 37–46 依序補上離線排程終端結果、12／24 Slot capability、預取截止、照片穩定排序、裝置狀態單調序號、昂貴 POST request fingerprint、診斷來源修復、legacy ambiguous 可見警告、`api_usage` 保留策略，以及完整照片庫 request-level Idempotency ledger。Migration 47–50 再加入 ledger reservation ownership lease、unknown 同步成本的 nullable 欄位語意、未修改預設政策的自動 API usage retention，以及有界 cleanup audit GC 索引。Migration 只能新增，已發布版本不可改寫。

## 執行架構

- `inktime-web`：Gunicorn／Flask、登入、RBAC、CSRF、管理 UI 與裝置 API；啟動時執行 Migration 與 Release reconciliation。
- `inktime-worker`：掃描、本地預處理、`local`／`single` 分析、OpenAI Batch import、渲染與有界背景工作。
- `inktime-scheduler`：排程、租約回收、跨日離線內容準備、通知、保留策略與備份。
- 三個程序共用 RuntimeConfig、SQLite WAL 與 `/data`，原始照片固定以 `/photos` 唯讀掛載。Web 不執行長時間圖片工作；Worker／Scheduler 等待 Web readiness。

正式新分析策略只有 `local` 與 `single`。舊 `low_cost`、`smart`、`smart_two_stage`、`high_quality`、`single_high` 只做輸入相容並正規化為單次完整分析，不會再次上傳第二張或第二個尺寸的圖片。`analysis.execution_mode` 預設 `local_only`；只有明確設成 `automatic_ai` 才會自動建立 Provider 請求。

## Renderer 與發布

- 8 種版型：`full`、`postcard`、`photo_info`、`photo_pair`、`photo_pair_caption`、`adaptive_memory`、`calendar`、`weather_sensor`。
- 3 種 Display Profile：`safe_4c`、`gdep073e01_6c`、`gdey073d46_7c`。
- 10 種抖動：`none`、`floyd_steinberg`、`gooddisplay`、`photo_smooth`、`atkinson`、`bayer4`、`bayer8`、`nearest`、`bayer_ordered`、`serpentine_floyd_steinberg`；色差為 `oklab` 或 `rgb`。
- Renderer 接受的來源圖片上限為 60,000,000 pixels，會先做 EXIF transpose／RGB 正規化與有界縮放。Preview 與正式 Release 共用 frozen Render Plan、版型幾何、Caption provenance、Profile 與 Effective Dither。
- Release 先 staging、驗證 Manifest／Payload，再以可補償流程切換 Profile pointer 與提交 SQLite；啟動 reconciliation 會隔離或標記不完整 Release，不會把未知檔案當成正式版本。

## 冪等、用量與資料生命週期

- 分析工作、維護掃描／渲染及完整照片庫操作使用 scoped `Idempotency-Key`、request fingerprint 與衝突回應，避免網路重送重複建立昂貴工作。
- 完整照片庫請求會在列舉照片前先用 `idempotency_requests` 取得 60 秒 reservation lease，owner 每 10 秒 heartbeat；只有目前 owner 能凍結 snapshot。相同請求可 replay／resume，不同 payload 重用同一 Key 或失去 lease 都會 fail closed。
- Provider 呼叫保存 `provider_reported`／`estimated`／`unknown` 成本來源；unknown 不冒充零成本，Migration 48 允許同步 unknown usage 的 `estimated_cost=NULL`。Migration 45 建立 `api_usage` 400 天／batch 200 政策；Migration 49 只把未經管理員修改的原始預設改為自動清理，且保留本月資料。管理員設定為 dry-run 的政策仍只評估、不刪除。
- Scheduler 以低頻維護執行 retention；Migration 50 為 cleanup audit history 建立索引，終態 audit 預設保留 90 天並每次最多 GC 10 筆，不為 GC 本身再建立 audit。
- Queue／Retention 清理會保護仍被 Queue Event、Release、Last Known Good、Rollout 或其他外鍵引用的父資料；不能為了達成保留天數破壞稽核鏈。

## 裝置與 PhotoPainter

- 自製韌體使用 possession-verified pairing、Device Secret 與 credential version；Legacy Bearer Token 與 Stock `/dataUP` 是獨立相容路徑。
- 交付模式為 `legacy_online`、`stock_compat`、`inktime_offline_schedule`。Enhanced 裝置必須 `offline_prefetch_allowed=true`；其他模式必須為 `false`。
- 未確認或舊裝置安全限制為最多 12 Slots；韌體 2.8.0 pairing capability 明確宣告 24 後才使用 24 Slots。`legacy_ambiguous` 會隔離並要求重新配對或 Repair，不能猜測能力。
- Offline Queue ACK 先寫入 ESP32 NVS 的 crash-consistent journal，再送 canonical `POST /api/device/v1/queue/ack`。只有伺服器接受與目前 Queue／Item／Release／事件身分一致的 `DISPLAY_COMPLETED` 才能完成 Item 並推進 current／Last Known Good；timeout、5xx、重啟、stale 或身分不符都保留 pending，不能提前解鎖下一張。
- Status 使用單調 sequence 與 server-fenced timestamp；未來時間、倒退 sequence 或非權威資料不能覆寫最新狀態。
- PhotoPainter Stock upload accepted 不等同實體面板已刷新；Enhanced 的 24-slot、BUSY、方向、色彩、GPIO5、deep sleep 與整板功耗仍需實機驗收。

## CI 與驗證邊界

GitHub Actions 是測試、建置、安全掃描、benchmark、韌體編譯與 hosted runtime 的權威環境。Draft PR 預設走 impact mode；Ready、`full-ci`、手動 full suite 或 `main` push 走完整模式。CI 必須分開報告 source-head provenance 與 PR merge-ref full validation，merge-ref 不可描述成 exact source-head full CI。

本頁只證明來源碼契約已盤點。真實 NAS、付費 Provider、真實 OpenAI／OpenRouter、ESP32／PhotoPainter 燒錄、面板刷新、BUSY、功耗、Wi-Fi 弱訊號、RTC／PMIC 與長時間 soak 未在本次文件同步中執行，均維持 `NOT RUN`。
