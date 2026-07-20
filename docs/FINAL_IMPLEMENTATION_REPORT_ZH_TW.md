# InkTime 最終實作與驗收報告

報告日期：2026-07-17  
分支：`feature/inktime-platform-hardening`  
實作基準：`b09baab415f155dcdb28f1ace57222227281045f`  
主要實作完成點：`166c3d66c28b6c2a9cdb046f88d1e9cb0f7d6e07`

## 結論與完成邊界

本次已把原本的單檔腳本型專案升級成具備登入、版本化 Migration、持久化背景工作、本地照片預處理、成本追蹤、繁體中文管理介面、版本化 2bpp 發布、裝置 Bearer Token、診斷、Docker 三服務與 CI 的可部署平台。核心安全、資料一致性、工作恢復、10 萬筆有界處理與容器首次啟動均有實測證據。

但原任務列出的全部進階 UI 與自動化項目尚未 100% 完成，因此本報告不宣稱所有 23 項完成條件均已完全達成。主要未完成邊界列於「未完成項目與已知限制」。這些不是空 UI；尚未完成的能力會明確保持關閉或不顯示為可用。

## 已完成項目與證據

| 領域 | 完成內容 | 主要證據 |
|---|---|---|
| 工程基線 | 現況稽核、現況／目標架構、Migration 與回滾計畫 | `PROJECT_AUDIT_ZH_TW.md`、`ARCHITECTURE_CURRENT.md`、`ARCHITECTURE_TARGET.md`、`MIGRATION_PLAN.md` |
| Web 安全 | 首次管理員、administrator／viewer、scrypt、Session、CSRF、登入限流與密碼變更 | `inktime/app/api/auth.py`、`inktime/app/web/access.py`、`tests/security/test_auth.py` |
| 路徑安全 | `resolve()`＋`relative_to()`；拒絕 traversal、絕對路徑、相似前綴與符號連結逃逸 | `inktime/app/core/paths.py`、`tests/security/test_paths.py` |
| 裝置驗證 | 每台獨立 Bearer Token、HMAC 雜湊、一次顯示、撤銷、停用、最後連線／IP、成功失敗計數 | `inktime/app/api/devices.py`、`repositories/devices.py`、`tests/security/test_device_tokens.py` |
| 工作系統 | jobs／items／events／errors、逐張狀態、有界 Future、暫停、續跑、取消、失敗重跑、租約恢復、預算停止 | `repositories/jobs.py`、`workers/job_worker.py`、`tests/integration/test_jobs.py` |
| 本地預處理 | SHA-256、pHash、dHash、EXIF、GPS、尺寸、亮度、對比、模糊、曝光、截圖特徵、重複繼承 | `domain/photos/preprocessing.py`、`workers/scanner.py`、`tests/unit/test_preprocessing.py` |
| 模型管線 | 512／1600px 分階段、一次回傳所有分析欄位、嚴格 Schema、文字修復最多一次、實際 usage | `services/analysis.py`、`domain/analysis/schema.py`、`tests/integration/test_analysis_pipeline.py` |
| Provider | OpenAI 相容即時 API、優先順序、RPM／TPM、並行、Retry-After、熔斷與故障轉移；Batch 提交／查詢／取消原語 | `providers/`、`services/providers.py`、`tests/unit/test_provider_router.py`、`test_provider_batch.py` |
| 設定與 Secret | settings／history／secrets、範圍驗證、重啟標記、Fernet 加密、遮罩、舊 config 匯入 | `repositories/settings.py`、`scripts/import_legacy_config.py`、`test_legacy_config_import.py` |
| 繁體中文 UI | 儀表板、照片與人工修正、工作、Provider、成本、渲染、裝置、設定、錯誤、診斷、備份、維護 | `inktime/app/web/templates/`、`tests/integration/test_management_ui.py` |
| 發布與 ESP32 | 四色 2bpp／六七色 indexed4、五種抖動、Profile latest、SHA-256、設定 ACK、離線／恢復通知與 Webhook | `domain/rendering/`、`services/notifications.py`、`esp32/ink-display-7C-photo/`、`test_palette.py` |
| 日期與字型 | 動態時區日期、閏年／2 月 29 日測試、CJK 字元覆蓋檢查、缺字明確失敗 | `domain/rendering/dates.py`、`fonts.py`、`tests/unit/test_dates.py`、`test_fonts.py` |
| 可觀測性 | human／JSON Log、穩定錯誤碼、聚合錯誤中心、live／ready／detail、已遮蔽診斷包 | `core/logging.py`、`api/health.py`、`services/diagnostics.py`、`ERROR_CODES_ZH_TW.md` |
| 部署 | Gunicorn、web／worker／scheduler、非 root、唯讀 Root、tmpfs、Healthcheck、Log Rotation、資源限制 | `Dockerfile`、`docker-compose.yml`、`DOCKER_GUIDE_ZH_TW.md` |
| CI | Ruff、Mypy、Pytest、pip-audit、Gitleaks、Docker health、Playwright、ESP32 compile、Migration | `.github/workflows/ci.yml` |

## 架構變更

```text
Browser / ESP32
       │
       ▼
Gunicorn + Flask API ── Service ── Repository ── SQLite WAL
       │                    │
       │                    ├── VisionProvider Router
       │                    ├── Thumbnail Cache
       │                    └── Atomic Release Publisher
       │
       ├── bounded Worker：scan / analysis / render
       └── scheduler：租約回收 / 排程備份
```

正式入口仍包覆舊 Flask app 以保留原有唯讀頁面與舊韌體遷移能力；新版 Route、Service、Repository、Provider 與 Worker 位於 `inktime/app/`。原分析器保留為 `legacy_analyze_photos.py`，`analyze_photos.py` 已成為建立新版持久化工作的相容包裝器。

## 資料庫變更與 Migration

- v1：library、photo、job、job item／event／error、usage、setting／history／secret、user、device、release、feature flag 等平台核心資料表。
- v2：結構化照片分析結果與必要索引。
- v3：本地影像特徵、Provider 與模型價格。
- v4：照片人工修正歷史與預設關閉的未來功能旗標。
- v5：版本化評分規則、綜合排序分與分析採用的規則版本。
- v6：ESP32 遠端設定、Heap／PSRAM／錯誤 Telemetry 與裝置事件。
- v7：完整六／七色 Profile、每台裝置設定版本／ACK、離線狀態與持久化通知／Webhook delivery。
- 每次連線啟用 WAL、foreign key、busy timeout 與 `synchronous=NORMAL`。
- Migration 逐版使用 `BEGIN IMMEDIATE`；跨程序檔案鎖避免多 Gunicorn Worker 首次啟動競爭。
- 舊資料庫升級前以 SQLite backup API 建立一致備份並執行 `quick_check`；失敗立即停止啟動。
- 舊 `photo_scores` 保留，不做破壞性刪除；舊設定可用 `scripts/import_legacy_config.py` 匯入。

## API 變更

主要新增：

- `/setup`、`/login`、`/logout`、`/account/password`
- `/api/v1/jobs`、`/api/v1/jobs/<id>/<action>`、estimate、export
- `/api/v1/photos/<id>` 人工修正與 `/api/v1/photos/<id>/image`
- `/api/v1/providers`、Provider 測試、`/api/v1/settings`
- `/api/v1/releases`、rollback、font upload
- `/api/v1/devices`、Token 重生、裝置更新／停用、Manifest 遠端設定與低頻狀態回報
- `/api/device/v1/releases/latest` 與版本化檔案下載
- `/api/v1/backups`、診斷包、錯誤解決與維護掃描
- `/health/live`、`/health/ready`、`/health/detail`

所有 mutation 由伺服器端角色與 CSRF 驗證。裝置 API 使用 Header Token，不接受 URL Token。

## ESP32 韌體變更

- AP 設定頁儲存伺服器與一次性裝置 Token，不把完整 Token印到序列埠。
- 先取得 Profile 專屬 Manifest，再驗證 schema、尺寸、2bpp／indexed4、面板、檔案大小與 SHA-256。
- 六／七色 payload 維持 192,000-byte 壓縮索引；設定驗證後寫入 NVS 並回報版本 ACK。
- 隨機檔案失敗會嘗試其他檔案；全部失敗時不刷新 framebuffer，保留正常舊畫面。
- CI 已加入 ESP32-S3、GxEPD2 與 ArduinoJson 編譯步驟；本機未另裝 Arduino CLI，因此本輪沒有本機實板／編譯結果。

## Token 節省方式與預估

1. SHA-256 完全相同照片直接繼承：該副本模型用量降低 100%。
2. 先本地擷取與品質判斷，避免把明顯截圖、文件、模糊或重複內容送到高品質階段。
3. 第一階段只傳 512px；通過門檻才傳 1600px，預設估計第二階段比例 35%。
4. 每個階段一次圖片請求同時產生描述、分類、分數與短文案；JSON 修復只傳文字且最多一次。
5. Provider usage 逐請求入庫；每日、每月、單工作、單張與最大輸出 Token 在送出前檢查。

相對於「每張照片兩次圖片請求」的舊流程，假設無重複且 35% 進入第二階段，圖片請求數由每 100 張約 200 次降為 135 次，約減少 32.5%。加入重複繼承與本地排除後，實際 Token／費用可再下降；合理情境估計約 30%～80%，但精確比例取決於相簿重複率、Provider 圖片計價與門檻，不能把此區間當成實際帳單保證。

## UI 頁面清單

- 首次設定、登入、密碼變更
- 儀表板
- 照片資料庫、照片詳細資訊、人工日期／類型／最愛／短文案修正
- 分析工作、工作詳細資訊與逐張狀態
- 模型與 Provider
- Token 與成本
- 電子紙渲染、字型、發布歷史與回滾
- ESP32 裝置
- 系統設定與停用中的 Feature Flags
- 錯誤中心
- 系統診斷與診斷包
- 備份與還原說明
- 系統維護與背景掃描

響應式 UI 已以桌面與 390px 行動版瀏覽器實際檢查，未發現水平溢位；畫面證據為 `docs/images/dashboard.png`。

## 安全修復清單

- 修正字串前綴式路徑 containment。
- Web UI 預設不再匿名開放；加入角色、Session、CSRF 與登入封鎖。
- Device Token 不進 URL，資料庫只存 HMAC；API Key 加密且 UI 遮罩。
- 診斷與結構化 Log 遞迴遮蔽敏感鍵。
- 舊 URL 金鑰 API 預設關閉，且設定需重啟才生效。
- Docker 以 UID 10001、唯讀 Root 與 `no-new-privileges` 執行。
- 升級 Flask、requests、Pillow、pillow-heif、cryptography、fonttools 至漏洞修復版本。

## 測試與驗收結果

### 本機聚焦驗收

- Ruff：通過。
- Mypy：59 個 source files，0 issues。
- Pytest：71 tests collected，全部非 E2E 測試通過；本機因未安裝 Playwright 套件，E2E module 略過。
- `git diff --check`：通過。

### Python 3.12 容器驗收

- Ruff：通過。
- Mypy：0 issues。
- Pytest：同一套 unit／security／integration 測試通過；需要外部 E2E URL 的測試略過。
- `pip-audit -r requirements.txt`：`No known vulnerabilities found`。
- Compose：web、worker、scheduler 皆以 `user=inktime`、`readonly=true`、0 restart 啟動；`/health/ready` 回傳 200。
- 兩個 Gunicorn Worker 首次同時 Migration：無 traceback、無重啟，容器 healthy。

GitHub Actions workflow 已建立，但本輪未 Push，因此不能宣稱遠端 Gitleaks、Playwright Chromium 與 Arduino 編譯 Job 已在 GitHub runner 實際通過。

## 100,000 筆效能結果

完整環境與限制見 `PERFORMANCE_REPORT.md`。最近一次結果：

- 照片中繼資料：100,000 筆。
- SQLite：約 87.03 MiB，完整性 `ok`。
- 批次寫入：17.572 秒，約 5,691 筆／秒。
- 深頁第 99,901 筆起查詢：351.89 ms，回傳 60 筆。
- 建立 100,000 個持久化 Job Item：0.986 秒，約 101,405 筆／秒。
- Worker claim 上限：8；不建立 100,000 個 Future。
- 租約回收：13.28 ms／8 筆。
- 最大 RSS：30.50 MiB；相對基線增加 9.06 MiB；單核心等效 CPU 34.9%。
- 取消後 claim：空集合，不再送新工作。

此測試只量測中繼資料與 Mock／本地路徑，不代表 NAS 圖片解碼或真實模型吞吐量。

## Docker 部署方法

```bash
cp .env.example .env
# 設定 INKTIME_DATA_PATH、INKTIME_PHOTO_PATH 與正式反向代理參數
docker compose up -d --build
curl --fail http://127.0.0.1:8765/health/ready
```

資料目錄需讓 UID 10001 可寫；照片目錄只讀掛載。正式環境用 HTTPS 並設定 `INKTIME_COOKIE_SECURE=1`。詳細步驟見 `DOCKER_GUIDE_ZH_TW.md`。

## 回滾方法

1. 暫停工作並建立、下載且驗證備份。
2. `docker compose down` 停止 web／worker／scheduler。
3. 還原先前映像版本與已通過 `quick_check` 的資料庫備份。
4. 修正 `/data` UID 10001 權限，先啟動 web 並確認 ready，再啟動 worker／scheduler。
5. 若需短期回到舊韌體，只能在隔離網路明確開啟舊 API；完成遷移後立即關閉。

## 未完成項目與已知限制

1. Batch Provider 原語已完成，但背景 Job 的自動切批、poll、結果匯入、取消與重啟恢復尚未串成端到端流程；目前正式分析工作使用即時 API。
2. 照片頁已有搜尋、狀態／類型／分數／重複篩選與人工修正，但尚缺完整的年份／月份／模型／錯誤複合篩選、網格清單切換及批次排除／刪除分析結果。
3. 工作建立頁是可用的精簡精靈，尚缺資料夾／日期／相簿／隨機抽樣等完整選片步驟、複製工作設定與 Batch 模式 UI。
4. 渲染已支援人臉／主體智慧裁切、Web 焦點調整、E6 適合度、五種固定相框版型、字型、實際色盤預覽、四色／六色／七色、五種抖動與 Profile 回滾；任意元件拖拉、縮放的自由版型編輯器尚未實作。
5. 備份可由 UI 建立、下載、驗證並排程；基於 SQLite 線上替換風險，還原仍依文件在服務停止狀態執行，尚未做成 UI 一鍵還原。
6. 裝置可建立、重生 Token、停用，並從 Web 編輯面板、時區、每日排程與 0°／180°；設定版本 ACK、離線／恢復通知、Firmware／RSSI／Heap／PSRAM／錯誤 Telemetry 已完成。指定圖片播放清單與簽章 OTA 尚未完成。
7. 診斷頁顯示 Provider 啟用數並提供逐一測試，但尚未建立所有 Provider 的定時主動健康探測與歷史圖表。
8. 儀表板目前提供核心指標與最近錯誤，尚未完成任務要求的全部趨勢圖表。
9. 部分 dashboard／operations Route 仍直接執行唯讀聚合 SQL，尚未完全達到「所有 Route 不直接 SQL」的最終分層目標。
10. 裝置通知 Webhook 已可用；Email、Telegram／LINE、S3、PostgreSQL、遠端／GPU Worker、人臉群組仍未實作，也不宣稱可用。
11. E2E 檔案涵蓋首次設定、登入、主要頁面與設定修改；原任務列出的工作控制、Provider 測試、渲染、裝置與備份完整瀏覽器流程尚未全部自動化。
12. Pillow 12.3 對 `getdata()` 發出 2027 年才移除的棄用警告；目前功能正確，後續應改用 `get_flattened_data()` 並保留相容測試。

## 未來建議

下一優先是簽章 OTA／金絲雀回滾與每台裝置播放清單，再補齊照片批次操作、自由拖拉版面與 Batch Job 生命週期；之後將 dashboard／operations SQL 移入 Repository，並只在需要多主機 Worker 時評估 PostgreSQL。SQLite 單主機部署應維持單一可靠本機 Volume，不要放在無鎖定保證的網路檔案系統。完整優先清單與 N100 實測見 `N100_IMPLEMENTATION_REPORT_ZH_TW.md`。

## Commit 清單

| SHA | 摘要 |
|---|---|
| `ddd55438c8f96a6c37a80ef10ea7d8534a2cde14` | docs: audit current platform and add migration baseline |
| `7324161cbae79e95f48e6322d1f06c9748e4e304` | feat: secure web access and device downloads |
| `2a3b824aa97e75e3dd1fc1993f44cc2d64c25199` | feat: add recoverable bounded background jobs |
| `38ac13e897207f2df627b3d4ec712aea39e0fbc4` | feat: add token-efficient photo analysis pipeline |
| `bfe3f74c4ed7f6bbe4967ddb6ddaab6ebf48ed64` | feat: add Traditional Chinese administration console |
| `1ef1709cdf8011bcaddea00bbbdd0e73f06b2831` | feat: publish verified 2bpp device releases |
| `daf33982fb542913ac039d9055c9df764c9bc19e` | feat: add observable provider routing and workers |
| `166c3d66c28b6c2a9cdb046f88d1e9cb0f7d6e07` | feat: complete deployment hardening and acceptance tooling |

本報告本身的文件 Commit 因 SHA 自我參照無法寫入自身內容；交付訊息會另列該最終 Commit SHA。
