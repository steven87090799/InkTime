# PhotoPainter 交付模式與相容邊界

本文件是目前程式碼、韌體與 Stock upstream 交叉核對後的操作契約。它把「既有 Online」、「Stock PhotoPainter 相容」與「InkTime Enhanced 離線排程」分開；三者不可用同一個預設值混淆。

## 1. 三種模式

| 模式 | 適用對象 | 交付來源 | 網路需求 | 預設行為 |
|---|---|---|---|---|
| `legacy_online` | 既有 generic device | `/api/device/v1/releases/latest` | 每次喚醒可連線 | Migration 27 對既有裝置的保留預設 |
| `stock_compat` | 明確選擇 Stock PhotoPainter Mode 1 的裝置 | InkTime Production BIN → server-side 24-bit BMP → Stock `/dataUP` | 管理端到同一 LAN 的 Stock Host | 不自動切換；不要求刷入 InkTime Enhanced 韌體 |
| `inktime_offline_schedule` | 明確選擇 Enhanced 韌體與離線排程的裝置 | Release → Queue Item → device local schedule | 預先準備時需要；顯示時可完全離線 | 需要 `offline_prefetch_allowed=true`、每日準備與裝置端正式 Frame |

建立或升級 generic device 不會自動變成 Stock；只有管理員在裝置設定選擇 `stock_compat` 才會啟用相容路徑。

## 2. Stock upstream 核對結果

本次核對的 Waveshare upstream 為：

- repository：[`waveshareteam/ESP32-S3-PhotoPainter`](https://github.com/waveshareteam/ESP32-S3-PhotoPainter)
- commit：`a5e8f757ba0cafbb5586f07d3e83bda3184c0845`
- Mode 1 基線：`01_Example/xiaozhi-esp32/components/user_app_bsp/mode_src/Basic_mode.cpp`
- Stock image directory：`/sdcard/06_user_foundation_img`
- config：`/sdcard/06_user_Foundation_img/config.txt`
- `timer` 是相對秒數，預設約 13 分鐘；upstream 沒有已驗證的任意絕對每日時刻清單。

因此 Stock 模式不宣稱支援 `08:00,12:00,20:00` 這類自訂絕對時刻。若要使用多個絕對時刻，請改用 `inktime_offline_schedule`，不要把 Stock config.txt 的相對 `timer` 誤當成每日時間表。

## 3. Stock `/dataUP` Payload

InkTime 不修改既有 Production BIN；轉換只發生在 Stock 交付邊界：

1. 讀取 Release Manifest 唯一且已授權的 `.bin` entry。
2. 先驗證 Release directory identity、檔名、大小與完整 SHA-256。
3. 依 `safe_4c` 或 `gdep073e01_6c` 的邏輯色碼轉成 exact logical RGB。
4. 將 portrait 480×800 依裝置 `rotation` 轉成 Stock 800×480。
5. 輸出 `mode byte + bottom-up 24-bit BMP`：BMP 1,152,054 bytes，完整 body 1,152,055 bytes。
6. 只送 `POST /dataUP`，`Content-Type: application/octet-stream`，不跟隨 redirect。

Stock logical RGB：black `(0,0,0)`、white `(255,255,255)`、red `(255,0,0)`、yellow `(255,255,0)`；六色另有 green `(0,255,0)`、blue `(0,0,255)`。七色 orange 不會被誤送為六色 Stock code。

管理端 API：

```text
POST /api/v1/devices/<device_id>/stock-photopainter/display
JSON: {"release_id": "...", "file_name": "..."}
```

回應中的 `upload_accepted=true` 只代表 Stock endpoint 回傳 2xx；`display_completed` 永遠保持 `false`，因為 Stock `/dataUP` 沒有 InkTime 可驗證的完成 callback。

## 4. Host、SSRF 與重送規則

`stock_endpoint_host` 只能是裸 LAN IP 或主機名，不可含 scheme、port、path、query、fragment 或 userinfo。每次上傳前重新解析 DNS；所有結果都必須是 private 或 link-local，public、reserved、loopback、multicast、unspecified 或 mixed DNS 會 fail closed。連線會固定到已驗證的解析位址，禁止 redirect。

HTTP timeout、read timeout、redirect、解析失敗或上傳後的未知回應都不會自動重送，避免 Stock 端已收到資料而 server 重複更新顯示。裝置事件只保存有界的 Release、檔名、大小、HTTP status 與結果，不保存 Token、Payload 或本機路徑。

## 5. Enhanced 離線排程契約

- Migration 27 建立 `device_offline_schedules`、`device_offline_schedule_slots` 與 queue 的 `offline_prefetch_allowed`。
- `schedule_times` 排序、去重，裝置端最多 12 個正式 Slot；`prefetch_lead_minutes` 為 0–120。
- 一日準備是單一 SQLite transaction：每個 Slot 恰好一個 Release、恰好一個 Queue Item、恰好一個完整 SHA-256；中途任一 Release 不合法時整批 rollback。
- 裝置使用 `GET /api/device/v1/offline-schedule` 取得 target date、timezone、config version、show-at 與完整 SHA-256；回應不含照片路徑或原圖。
- 延遲 ACK 例外只允許 `DISPLAY_STARTED`、`DISPLAY_COMPLETED`、`DISPLAY_FAILED`，且 Queue Item 必須是 offline prefetched、已經 ACK/display 狀態、Release identity 與 deadline 都相符；一般 Online Queue 仍採嚴格 queue-version。
- Enhanced 裝置在正式 Frame 可用時以 RTC-first、exact epoch deep sleep 規劃下一個 prefetch/display 時刻；local-only wake 不呼叫 Wi-Fi、NTP、Manifest 或 Status。

## 6. 驗證狀態

已在隔離 worktree 完成 server unit/integration、Review UI、Stock bytes、LAN transport policy、offline transaction、Queue ACK、host C++ core 與兩個 board profile 的 host compile。真實 PhotoPainter、面板 BUSY、PMIC、SD、電池、LAN endpoint、真實 OpenAI/NAS 與 Arduino hosted compile/physical flash 本輪均為 `NOT RUN`；不把 simulator、mock 或 host compile 當成實機 PASS。
