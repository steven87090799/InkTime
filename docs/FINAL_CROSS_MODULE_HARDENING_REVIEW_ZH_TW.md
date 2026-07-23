# 最終跨模組一致性、安全性、低功耗與發布可靠性稽核

基準：`origin/main` 65c471b；2026-07-22 main CI run 29930737301 成功。PR #20 已合併且 python-quality 成功。

| 分類 | 實際位置／目前行為 | 真正風險與本 PR | 測試證據／硬體邊界 |
|---|---|---|---|
| `confirmed` | `repositories/render_candidates.py`、`RenderService.publish` | 原一般/歷史/排程資格不一致；已統一 analyzed、eligible、active、啟用 Root、最新分析、檔案存在，明確指定失敗為 `RENDER-009` | 候選與歷史整合測試；實體 NAS 權限仍需驗證 |
| `confirmed` | `DisplayPrepareConfig`、`DisplayPreparationService`、Worker render handler | 原 `display_prepare` 只存 JSON；已嚴格解析全部八欄、解析裝置 Profile、年份與數量，Release 完整後才成功 | DTO、Scheduler/Worker 測試 |
| `confirmed` | `RenderService.reroll_history_day` | 原先 SQL `LIMIT 1000` 後才在 Python 選；已改 500 筆有界走訪、SQL final-score 排序、unseen pool 與 weighted reservoir | 10k/100k 合成測試；不是 NAS 解碼量測 |
| `confirmed` | `ReleaseCoordinator`、`AtomicReleasePublisher.validate/activate_manifests` | 原多 Profile 逐一切 pointer/DB；已 staged、驗證、pointer snapshot、補償、published+history transaction；啟動 reconciliation 可將失效 pointer 回復到同 Profile 最新完整版本 | Release/recovery 測試；多磁碟斷電仍需 NAS 測試 |
| `confirmed` | `BoundedJobWorker.run_job`、`JobRepository.record_late_completion` | 原 timeout 移除仍執行 Future；現在停止 claim、保留追蹤、late result 標 `timed_out_completed` 且不重試 | Worker timeout 測試；Python Thread 仍採 cooperative cancellation |
| `confirmed` | `ai_cache_reservations`、`PhotoAnalysisService._model_call` | cache miss 競態可重複付費；已用 DB lease single-flight、有界等待與失敗接手 | 多 Thread reservation 測試、Fake Provider 邊界 |
| `already_mitigated` | `PhotoPreprocessor.analyze` | Metadata 已用 Pillow/pillow-heif，沒有 ExifTool；本 PR 加 AST 與特殊檔名測試 | `test_no_shell_metadata.py` |
| `already_mitigated` | `Database` | 已有 WAL、busy timeout、NORMAL、FK、跨程序 writer lock；本 PR 增加 writer wait/max、timeout count、WAL bytes 與長交易警告，未建立第二套 queue | SQLite concurrency；目標 NAS 的實際指標仍需持續觀察 |
| `confirmed` | device manifest/file/status API、`DeviceTestReleaseStore` | 原 BIN 未完整驗證且下載即 consumed；已驗 Manifest/size/SHA/Profile、失敗 rate limit、ACK 後 consumed | device token/ACK 整合測試 |
| `already_mitigated` | `AtomicReleasePublisher` | 正式 BIN 已在伺服器預先產生，ESP32 不做照片量化 | Renderer/Release 測試 |
| `confirmed` | `DisplayProfile.panel_capabilities`、`power_policy.h` | 六／七色不可假定局刷；全部設定 false/full refresh，加入最小間隔純邏輯 | host C++ `-Werror -pedantic`；面板 waveform 未實測 |
| `partially_present` | PhotoPainter `photopainter_support.cpp` | 已檢查 PSRAM/Flash、BUSY timeout、低電壓與集中 sleep；wire/native buffer 尚未完全改為 begin-time 唯一 owner | Compile/host tests；需實體 PSRAM 壓力與 USB 長駐測試 |
| `partially_present` | `.ino`、`power_policy.h` | 已避免一般喚醒清除 Wi-Fi 持久設定並定義 battery/USB budget；cached BSSID/channel fast-connect 尚未接入 NVS 實作 | host policy tests；需 AP/弱訊號/功耗量測 |
| `partially_present` | transport guard | 正式模式拒絕未配置 CA 的 HTTPS且無 `setInsecure()`；可信 CA provisioning 尚未完成 | 靜態搜尋；跨網路使用 VPN，見傳輸文件 |
| `deferred_with_reason` | OTA | 目前 partition/rollback/signature/低電壓實體證據不完整，不加入半套 OTA | 見 `SECURE_OTA_DESIGN_ZH_TW.md` |
| `partially_present` | `.ino` 主流程 | HTTP/NTP/BUSY/AP 多數已有 timeout/yield；尚未完整拆成 DeviceStateMachine 與 Task WDT | 五種 CI compile 尚需本 PR CI；實體 WDT fault injection 未完成 |

## 已確認不存在或已處理

- 沒有 ExifTool Metadata CLI、`os.system`、`os.popen` 或 `shell=True` production 路徑。
- 裝置認證已是 Bearer Token，沒有改成不相容 Header。
- SQLite 共用連線層已具備 WAL、10 秒 busy timeout、NORMAL、foreign keys 與 writer lock。
- 正式 Release 已是 Server Renderer 預產 BIN。
- PR #20 已修復 python-quality 基線；不得沿用舊失敗判斷。

## Migration、相容性與回滾

Migration 15 新增 Release reconciliation 欄位、Job idempotency/completion state、AI reservation 與裝置驗證失敗表。Flask、SQLite、既有 Job/Manifest/Profile/Bearer 契約保留。回滾前停止 Web/Worker/Scheduler，以 pre-migration 或正式備份還原 DB，再切回舊映像；`/data/releases` 需一併使用 NAS snapshot 回復。

## 未完成的實體驗證

實體六／七色刷新、顏色、BUSY polarity、低電壓保畫面、PSRAM largest block/長駐 USB、BSSID fast connect、NTP/HTTP 分段 wake duration、電池續航、真實 NAS WAL 與 100k 原圖掃描均未由本 PR 的軟體測試證明。不得宣稱固定續航百分比或 API 延遲保證。

## 本機驗證證據（2026-07-22）

- Python 3.12 Linux 容器：完整 unit/security/integration coverage 為 200 passed、1 skipped，總覆蓋率 81%；migration 10 passed；`pip-audit` 無已知漏洞。
- 新增的 single-flight、SQLite concurrency、Release recovery、排程、多模式重抽、裝置 ACK、Metadata shell 邊界與 Worker timeout 測試各重跑兩輪。
- Docker 以全新暫存資料目錄建置成功，`/health/ready` 回傳所有 checks=true；啟動 reset 後健康。
- 100,000 筆合成歷史候選案例單獨執行牆鐘 5.59 秒、peak memory footprint 160.47 MiB；此數字包含 fixture 建立，並非純 SQL 或 NAS 原圖掃描時間。
- Host C++ pure-logic 以 `-Wall -Wextra -Werror -pedantic` 通過。五種 Arduino Profile、Playwright、Gitleaks 與 Python 3.10 交由本 Draft PR CI 驗證；建立 PR 時不得先宣稱成功。
