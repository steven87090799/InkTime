# Docker／N100／ESP32 最終實作與驗收報告

日期：2026-07-18
目標平台：Intel Processor N100、Docker Engine 24+、Compose v2、Linux x86_64

這份報告記錄本次真正完成的變更、資源瓶頸判斷、隔離 Docker 實測與後續功能優先順序。部署時使用的固定規格見 [Docker 部署規格](DOCKER_GUIDE_ZH_TW.md)，日常維運見 [N100 資源指南](N100_RESOURCE_GUIDE_ZH_TW.md)與 [Log 指南](LOGGING_GUIDE_ZH_TW.md)。

## 結論

InkTime 已拆成 Web、Worker、Scheduler 三個健康檢查完整的容器程序；N100 預設只讓分析工作並行 1、預取 1，待機時 Worker 每 15 秒、Scheduler 每 60 秒才檢查一次。圖片特徵不再展開完整 4K／8K RGB 與灰階副本，工作進度也不再逐張寫入 Docker Log。

日常分析、模型、成本、渲染、裝置、排程、備份、Session、Log 層級與效能參數都能從 Web 修改。只有 Volume、主機 Port、映像 Tag、HTTPS、容器 CPU／RAM／PID 上限與 Docker Log 輪替屬於啟動前的部署邊界；容器內應用不應取得 Docker Socket 去改這些值。

## 已完成項目

| 範圍 | 實作結果 |
|---|---|
| Docker | 單一正式映像、Gunicorn、非 root、唯讀 Root、tmpfs、init、graceful stop、三服務健康檢查 |
| N100 限制 | Web 0.75 CPU／384 MiB；Worker 2 CPU／1 GiB；Scheduler 0.25 CPU／192 MiB；均有 PID 限制 |
| 待機 | Web 1 worker × 2 threads；Access Log 預設關閉；Worker 15 秒、Scheduler 60 秒輪詢 |
| 圖片記憶體 | Decoder 先 draft／thumbnail 到最長邊 512px，再建立 RGB／灰階特徵樣本；模糊度用串流統計 |
| CPU | pHash 改成預先計算、可分離的 DCT；避免每張圖片重複巢狀三角函數 |
| 工作佇列 | 有界 Future、預取倍數 1、每 30 秒續租；進度依張數或時間節流 |
| SQLite | WAL、busy timeout；沒有待執行 Migration 時不再為三個程序各做一次無意義啟動備份 |
| Web 設定 | Provider 完整限制、全部系統設定、設定稽核、裝置新增／編輯、裝置 Telemetry、主機／程序／cgroup 診斷 |
| ESP32 | Bearer Token、六／七色 Profile、五種抖動、設定 ACK、離線／恢復通知、SHA-256、Telemetry、Deep Sleep |
| Log | JSON／human、DEBUG 至 CRITICAL、結構化事件、工作開始／節流進度／完成／錯誤；5 MiB × 3 輪替 |

## 瓶頸與處理

| 原風險 | 為什麼會形成瓶頸 | 現在的處理 | 仍需觀察 |
|---|---|---|---|
| 24MP 圖片完整解碼 | RGB、灰階、像素陣列可能同時存在 | 特徵階段先縮至 512px；原始尺寸與 EXIF 仍保留 | 損壞 HEIC、極端大 PNG 的 decoder 行為 |
| 舊 pHash DCT | Python 巢狀迴圈反覆計算 cosine | 預算 cosine 並做兩次 1D DCT | 大量首次掃描時仍是 CPU 工作 |
| 無界工作預取 | Future 與圖片工作項目堆在 RAM | `concurrency × queue_multiplier` 有界 | 並行 2 前先以真實照片量測 |
| 2 秒空轉輪詢 | N100 長期被喚醒、SQLite 增加讀取 | Worker 15 秒，Scheduler 60 秒，可由 Web 動態調整 | 反應時間與低功耗之間的取捨 |
| 每張成功 Log | 大相簿會產生大量磁碟寫入 | 預設只輸出彙總進度與取樣錯誤 | DEBUG 只短期開啟 |
| 啟動重複備份 | 三個容器啟動造成不必要 I/O | 只有存在待執行 Migration 才備份 | 升級前仍應手動建立可下載備份 |
| Compose 重複建置 | 共用錨點帶 `build` 會為三服務重跑相同 Build | 只由 Web 建置一次，Worker／Scheduler 重用同一 image ID；Build metadata 放在依賴層之後 | 變更 `requirements.txt` 時才需重裝 Python 套件 |
| 診斷目錄計算 | 每次開頁遞迴掃描快取 | 目錄大小結果預設快取 300 秒 | 快取目錄達數百 GB 時調高快取秒數 |

## 2026-07-18 隔離驗收數字

以下在 Apple Silicon／OrbStack Linux ARM64 的隔離資料目錄量測，目的是驗證容器限制、健康、待機行為與記憶體量級；它不是 Intel N100 的耗電或效能保證。N100 實機上線後應依 [N100 資源指南](N100_RESOURCE_GUIDE_ZH_TW.md)再跑一次相同步驟。

| 項目 | 實測 |
|---|---:|
| 映像大小 | 約 306 MB（約 292 MiB；文件調整會造成少量差異） |
| Web 待機 | 50.32 MiB／384 MiB，瞬時 CPU 0.04%，5 PIDs |
| Worker 待機 | 34.66 MiB／1 GiB，瞬時 CPU 0.01%，2 PIDs |
| Scheduler 待機 | 34.48 MiB／192 MiB，瞬時 CPU 0.00%，2 PIDs |
| 三容器合計 | 約 119.46 MiB；單次瞬時 CPU 合計約 0.05% |
| 健康狀態 | 三容器 healthy、0 restart；Ready 的 DB／Migration／設定／Worker／發布目錄全通過 |
| 待機觀察 Log | 只有平台、Worker、Scheduler 啟動事件；沒有健康檢查 Access Log 或空轉輪詢 Log |
| GDEY 2.2.0 韌體編譯 | 1,211,257／1,310,720 bytes（92% Flash）；全域變數 96,556 bytes（29%） |
| GDEP 2.2.0 韌體編譯 | 1,211,329／1,310,720 bytes（92% Flash）；全域變數 96,564 bytes（29%） |

合成 6000×4000 RGB 圖片的本機記憶體路徑比較中，舊式完整 RGB／灰階展開路徑峰值約 299.97 MiB；新的完整 `PhotoPreprocessor` 峰值約 27.61 MiB。這是防止大圖副本的工程驗證，不代表真實 HEIC／JPEG 相簿的固定峰值。固定種子雜訊樣本的 pHash 新舊結果相同，局部 microbenchmark 由約 15.41 ms 降至 1.45 ms。

## N100 正式建議

[Intel 官方規格](https://www.intel.com/content/www/us/en/products/compare.html?productIds=88183%2C231803)列出 N100 為 4 核心／4 執行緒、最高 3.4 GHz、6 MB Cache、6 W Processor Base Power。建議先維持本專案預設：

- 主機至少 8 GB RAM；若同機還有照片服務、反向代理或資料庫，建議 16 GB。
- `analysis.concurrency=1`、`worker.queue_multiplier=1`；100 張真實照片壓測峰值低於 Worker 上限 60% 才試並行 2。
- SQLite、`/data` 與 Docker Log 使用 SSD；原始照片可在 NAS，但掛載必須唯讀且網路中斷時不能讓掃描無限重試。
- N100 BIOS／Linux governor、SSD、NAS 與電源供應器會主導整機瓦數；本專案只能降低 CPU 喚醒、RAM、I/O 與網路活動，不能宣稱固定整機功耗。

## 已完成的原 P0 項目

- **裝置離線／恢復通知**：站內歷史、轉態去重、可選冷卻提醒、持久化三次 Webhook 重試與加密 Bearer Token。
- **完整 6／7 色渲染 Profile**：GDEP 六色、GDEY 七色、舊四色相容、OKLab／RGB 與 Floyd–Steinberg／Atkinson／Bayer／none。
- **設定版本 ACK**：每台裝置期望與已套用版本、NVS 保存、Profile 安全拒絕與 Web 狀態。

詳細規格見[裝置可靠性與六／七色渲染指南](DEVICE_COLOR_NOTIFICATION_GUIDE_ZH_TW.md)。

## 建議增加的功能

### P0：最值得先做

1. **簽章 OTA 與分批發布**：ESP32 韌體簽章、裝置群組、10% 金絲雀、回滾與版本相容矩陣。
2. **更多通知通道與系統事件**：在目前 Webhook 基礎上加入 Email、LINE／Telegram，並涵蓋連續刷新失敗、備份失敗與磁碟不足；沿用現有節流與去重。
3. **色彩校正檔**：依實際面板批次、環境溫度與測試圖建立可匯入的校正 Profile；軟體預設 RGB 不能替代實測。

### P1：提升日常體驗

1. 拖拉式電子紙版面編輯器、預覽、節日模板與每台裝置獨立播放清單。
2. 電池電壓／電量曲線、刷新耗時與 Deep Sleep 續航估算已完成；Wi-Fi 分段耗時與實機
   電流校正仍待硬體量測。
3. 重複照片審核工作台、相似照片群組與安全的人工保留／忽略流程；不直接刪原始照片。
4. Provider 成本趨勢、Rate Limit 預測、工作完成時間估算與自動選擇低成本模型。
5. Prometheus／OpenTelemetry 唯讀指標端點；高基數欄位與照片 ID 不可直接當 Label。

### P2：資料量或部署規模變大後

1. 10 萬筆以上列表改用 keyset pagination，掃描支援增量檔案事件與週期性校正。
2. 多 Worker／遠端 Worker 前先把 SQLite 單機邊界明文化；跨主機才評估 PostgreSQL 與物件儲存。
3. 人臉群組、地點地圖與家庭成員分享必須先完成隱私、刪除、權限與模型資料去向設計。

這些項目目前是建議，不應在 Web 介面顯示成已完成能力；現有未來功能旗標保持預設關閉。

## 已知邊界

- GDEY073D46 已由原廠標示 EOL；新採購以 GDEP073E01 為優先，接線與溫度範圍不可混用。
- ESP32 韌體在 CI 編譯兩個 7.3 吋 Profile；實際上屏前仍須以對應面板、轉接板與穩定 3.3 V 電源做硬體驗收。
- 兩個 ESP32 Profile 的 app partition 都已使用 92%；新增 OTA／TLS／大型配對頁前必須規劃 Flash partition、回滾空間並重新做實機記憶體驗收。
- Web 不能安全改 Docker Volume、Port、CPU／RAM 上限或 HTTPS 憑證；這些值仍由 `.env`、Compose 與反向代理管理。
- 本報告沒有聲稱 N100 實機瓦數；部署後應量測 15 分鐘待機、100 張照片掃描與 100 張模型分析三種情境。
