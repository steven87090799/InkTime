# ESP32-S3 與電子墨水完整指南

## 1. 正式支援範圍

| 組合 | 狀態 | 韌體選項 | InkTime 發布格式 |
|---|---|---|---|
| ESP32-S3＋GDEY073D46 7.3 吋 | 已支援；面板原廠標示 EOL | 預設編譯 | 七色 indexed4、192,000 bytes；亦相容四色 2bpp |
| ESP32-S3＋GDEP073E01 7.3 吋 Spectra 6 | 新採購建議；已加入 GxEPD2 編譯選項 | `INKTIME_PANEL_GDEP073E01=1` | 六色 indexed4、192,000 bytes；亦相容四色 2bpp |
| Waveshare ESP32-S3-PhotoPainter＋7.3 吋 E6 | 軟體 adapter 已加入；尚待實機驗證 | `DEVICE_PROFILE=DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER` | 既有 480×800 wire payload 轉面板原生 800×480 六色 4bpp |
| `esp32/ink-display-133C-photo` 13.3 吋 | 舊實驗韌體，不是正式安全路徑 | 不建議部署 | 舊 1200×1600 split 4bpp，與新版 Manifest 不相容 |

GDEY073D46 原廠資料為 800×480、7 色、3.3 V、50-pin FPC、SPI、15–35°C、全刷約 32 秒，且頁面已標示 EOL。[Good Display GDEY073D46](https://www.good-display.com/product/442.html)

未來新採購建議 GDEP073E01＋DESPI-C73 或原廠 ESP32E6-E01。GDEP073E01 為 800×480、6 色 Spectra 6、3.3 V、50-pin SPI、0–50°C、全刷約 15–22 秒；韌體已可選用 GxEPD2 的 `GxEPD2_730c_GDEP073E01` 類別。[Good Display GDEP073E01](https://www.good-display.com/product/533.html) [DESPI-C73 adapter](https://www.good-display.com/product/522.html) [GxEPD2 支援清單](https://github.com/ZinggJM/GxEPD2)

伺服器、Manifest schema v2 與韌體 2.2.0 已共同支援完整六／七色；色盤、五種抖動、混合面板發布、設定 ACK 與離線通知詳見[裝置可靠性與六／七色渲染指南](DEVICE_COLOR_NOTIFICATION_GUIDE_ZH_TW.md)。

Waveshare 整合的中央 Profile、SD／PMIC／RTC／SHTC3、安全 BUSY timeout、授權與
20 項實機清單見 [PhotoPainter 支援與實機驗收](WAVESHARE_PHOTOPAINTER_ZH_TW.md)。

## 2. 建議硬體清單

- ESP32-S3 開發板或模組，建議 8 MiB PSRAM、8 MiB 以上 Flash。
- GDEP073E01＋DESPI-C73（新採購），或既有 GDEY073D46 相容驅動板。
- 穩定 3.3 V 電源與共地；SPI data line 也必須是 3.3 V，不可直接接 5 V。
- USB 線、瞬時電流足夠的電源、短且固定良好的 SPI 線。
- 若使用電池：低靜態電流 regulator、電池保護、正確充電 IC；不要把裸 Li-ion 直接接 3.3 V rail。
- Factory reset 按鍵：GPIO38 對 GND，使用內部 pull-up。

ESP32-S3 官方資料列出的 Wi-Fi TX 峰值最高可到約 340 mA；SoC deep sleep 在特定 RTC domain 條件下約 7–8 µA，但模組 PSRAM、regulator、LED、USB bridge 與電子紙驅動板會增加整板耗電。不要把 7 µA 當成成品保證值。[ESP32-S3 datasheet](https://documentation.espressif.com/esp32-s3_datasheet_en.pdf)

工程建議是讓 3.3 V rail 對 Wi-Fi 峰值、面板 boost 與裕量都有空間；若自製電源，1 A 等級是保守起點，但最後必須以你的 ESP32 模組、adapter datasheet 與實測波形決定。大尺寸面板不可只靠訊號腳供電，也不要把裸 50-pin FPC 直接接 ESP32；必須使用對應 adapter／boost 與 waveform 電路。

## 3. 本專案 7.3 吋接線

以下 GPIO 來自 `ink-display-7C-photo.ino`，適用目前 PCB／線路；換開發板前先查 strapping、Flash／PSRAM 占用與原理圖。

| 電子紙訊號 | ESP32-S3 GPIO | 說明 |
|---|---:|---|
| BUSY | 14 | 面板忙碌輸出 |
| RST | 13 | 面板 reset |
| D/C | 12 | data／command |
| CS | 11 | SPI chip select |
| SCLK | 10 | SPI clock |
| DIN／SDI | 9 | SPI MOSI |
| GND | GND | 必須共地 |
| 3.3V | 3.3V supply | 依 adapter 規格供電 |
| Factory reset | GPIO38 → GND | 開機按住清除 NVS，進入 AP 配對 |

DESPI-C73 對 MCU 暴露 BUSY、RES、D/C、CS、SCK、SDI、GND、3.3V；FPC 方向接反可能損傷面板。斷電後再插拔 FPC，鎖緊 connector，再上電。

## 4. 編譯

Arduino CLI：

```bash
arduino-cli core update-index
arduino-cli core install esp32:esp32@3.3.10
arduino-cli lib install GxEPD2@1.6.9 ArduinoJson@7.4.3

# 既有 GDEY073D46
arduino-cli compile --fqbn esp32:esp32:esp32s3 esp32/ink-display-7C-photo

# 新 GDEP073E01
arduino-cli compile --fqbn esp32:esp32:esp32s3 \
  --build-property "compiler.cpp.extra_flags=-DINKTIME_PANEL_GDEP073E01=1" \
  esp32/ink-display-7C-photo
```

Board 選 ESP32-S3，啟用 OPI PSRAM。正式版 `INKTIME_DEBUG_LOG=0`；短期硬體除錯才加入 `-DINKTIME_DEBUG_LOG=1`。序列 Log 不輸出 Token，但正式環境仍不應長期開啟。PhotoPainter 必須使用 16 MiB Flash／OPI PSRAM 與中央 `DEVICE_PROFILE`，完整命令見上方專用指南。

2026-07-19 以 Arduino CLI 1.5.1、ESP32 core 3.3.10、GxEPD2 1.6.9、ArduinoJson 7.4.3 實際編譯 2.4.0：GDEY Profile 使用 1,213,069 bytes、GDEP Profile 使用 1,213,141 bytes，兩者都是預設 1,310,720-byte app partition 的 92%；全域變數均為 96,564 bytes（29%）。這表示目前可燒錄，但 Release Flash headroom 只有約 7.5%，Debug 更達 98%；新增 OTA、TLS certificate 或大型 Web UI 前必須重新檢查 partition、實際板上 Flash 與 OTA 雙分區，不能只看模組標示的總 Flash。編譯器顯示約 231 KB 可用動態記憶體不包含執行期碎片與 TLS buffer；下載索引改放 PSRAM，板上仍必須啟用並檢查 PSRAM。PhotoPainter 的完整矩陣與實機邊界見專用指南。

上傳前先用原廠 sample／GxEPD2 Example 驗證「面板型號＋adapter＋供電＋引腳」能完整刷新，再燒 InkTime 韌體。不同面板 driver class 不可混用。

## 5. 首次 AP 配對

1. 在 InkTime Web「裝置」按「新增裝置」，立即複製只顯示一次的 `itd_...` Token。
2. 首次開機或按住 GPIO38 再上電，裝置建立 `InkTime-XXXXXX` AP，5 分鐘未儲存會睡眠。
3. AP 密碼為 `InkTimeXXXXXX`，其中 `XXXXXX` 與 SSID 尾碼相同；每台不同，不再使用共用 `12345678`。
4. 連到 AP，瀏覽 `192.168.4.1`。
5. 填 Wi-Fi SSID／密碼、InkTime URL（例如 `http://192.168.1.20:8765`）與裝置 Token。
6. 儲存後裝置重啟、連 Wi-Fi、同步 NTP、取得 Manifest、下載並驗證圖片、刷新後 deep sleep。

Wi-Fi、伺服器 URL 與 Token 是尚未連網前的 bootstrap，無法從 InkTime Web 遠端設定。裝置一旦能連線，面板 Profile、時區、每日 HH:MM、0°／180°旋轉與啟停都在 InkTime「裝置」頁管理；下一次取得 Manifest 時自動套用並寫入 NVS，再以設定版本 ACK 證明已生效。

Token 只存於 ESP32 NVS 與 server 雜湊，不進 URL、不印到序列埠。Token 遺失或懷疑外洩時在 Web 重生；舊 Token 立即失效。

## 6. 網路協定

1. `GET /api/device/v1/releases/latest`，Header：`Authorization: Bearer <token>`。
2. 驗證 Manifest schema 1／2、`pixel_format=2bpp`／`indexed4`、Profile、`width=480`、`height=800`。
3. 套用 `device_config` schema v2：設定版本、面板 Profile、IANA 時區換算後的 UTC offset、`HH:MM`、rotation。
4. 隨機選檔；失敗時嘗試下一個。
5. 四色檔必須剛好 96,000 bytes、六／七色必須 192,000 bytes且 SHA-256 相符；維持壓縮索引，不展開 384,000-byte framebuffer。
6. 完整成功才刷新面板；全部失敗保留舊畫面。
7. `POST /api/device/v1/status` 回報 firmware、面板／渲染 Profile、release、設定 ACK、RSSI、free heap／PSRAM、wake reason、是否刷新與錯誤碼。

Web 裝置頁會顯示最後狀態、下載成功／失敗、韌體、訊號、Heap、PSRAM 與最近事件；Docker INFO Log 只記每日狀態，檔案下載細節在 DEBUG 才出現。

## 7. 低功耗行為

- 開機 CPU 設為 80 MHz；Wi-Fi TX power 降低並啟用 Wi-Fi sleep。
- 只有下載與刷新期間保持清醒；之後關閉 Wi-Fi／Bluetooth、釋放 96,000／192,000-byte 壓縮索引。
- 面板 `hibernate()`，EPD GPIO 轉 pulldown／hold，ESP32 timer deep sleep 到下一次 HH:MM。
- NTP 失敗時最多睡 24 小時，不進入高頻重試迴圈。
- AP 配對 5 分鐘逾時後睡眠，避免未配置裝置永久開 AP。
- 電子紙為 bistable，斷電仍保留畫面；不要為「維持畫面」保持 ESP32 清醒。

實際電池續航必須量測整板平均電流：至少包含一次 Wi-Fi 連線、HTTP 下載、15～32 秒面板刷新、23 小時以上 deep sleep，以及弱訊號重試。每日刷一次通常比每小時刷一次省電得多。

## 8. 電子紙注意事項

- GDEY073D46 正常操作溫度只有 15–35°C；GDEP073E01 為 0–50°C。超出範圍不要硬刷，低溫 waveform、刷新時間與顏色都可能異常。
- 全彩電子紙刷新慢是正常現象；BUSY 期間不可斷電、reset 或重送 frame。
- 不支援把同一張 480×800 payload 直接拿去 13.3 吋；尺寸、pixel format、driver、split layout 都不同。
- 面板脆弱，避免彎折、點壓、扭曲與 FPC 拉扯；不要撕除非原廠指示可移除的保護層。
- 電子紙可能有 ghosting／色偏；本韌體採 full refresh，不把 GxEPD2 partial window API 當成 GDEY 可用的快速局刷。
- 強烈建議用 SHA-256 驗證與「成功才刷新」；不要為省幾秒移除。
- 完整六／七色需 server、裝置 Profile 與 2.2.0 韌體配對；舊韌體升級期間使用 `safe_4c`。
- 目前 app partition 已使用 92%；增加函式庫、TLS 或 OTA 前要重新量測 Flash／Heap／PSRAM，並做實機連續刷新與斷電恢復測試。

## 9. 常見錯誤

| Web／Docker 錯誤 | 原因 | 處理 |
|---|---|---|
| `DEVICE-CONFIG` | URL 或 Token 未設定 | 重新進 AP 配對 |
| `DEVICE-MANIFEST-HTTP` | 401／404／網路／代理 | 查 Token、是否已發布、N100 IP 與防火牆 |
| `DEVICE-MANIFEST` | schema／pixel format 不相容 | Server 與 firmware 版本配對 |
| `DEVICE-DISPLAY-MISMATCH` | 不是 480×800／無檔案 | 重新發布正確 profile |
| `DEVICE-CONFIG-PROFILE` | 設定版本倒退或面板 Profile 與韌體不符 | 核對面板型號、編譯 flag、裝置頁 Profile |
| `DEVICE-MEMORY` | PSRAM 未啟用或不足 | Arduino 啟用 PSRAM、檢查板型／供電 |
| `DEVICE-DOWNLOAD` | 長度或 SHA-256 失敗 | 查 RSSI、代理快取、N100 Log |
| 畫面不刷新 | BUSY／接線／供電／driver class 錯 | 先跑原廠 sample，量 3.3 V，核對面板型號 |
| 顏色錯誤 | 面板類別或 palette 不符 | 核對 GDEY／GDEP compile flag，不混用 13.3 driver |

## 10. 公網安全邊界

目前韌體支援 URL 形式的 HTTP／HTTPS，但沒有在 Web 配置 CA certificate 的完整流程。隔離 LAN 可用 HTTP；跨網路請用 VPN／IoT VLAN 或在韌體加入受信任 CA／憑證釘選後才使用 HTTPS。不要使用 `setInsecure()` 當正式解法，也不要重新啟用 URL key 舊 API。
