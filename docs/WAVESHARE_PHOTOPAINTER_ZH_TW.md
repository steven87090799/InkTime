# Waveshare ESP32-S3-PhotoPainter 支援與實機驗收

## 支援狀態

InkTime 2.4.0 可用單一 compile-time Profile 切換既有 PCB 與 Waveshare
ESP32-S3-PhotoPainter。這是可編譯、可靜態測試的硬體 adapter；目前沒有連接
真實 PhotoPainter、面板、SD、電池或量測儀器，因此不得視為實機驗證完成。

```cpp
#define DEVICE_PROFILE DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER
```

Arduino CLI 以 `compiler.cpp.extra_flags` 傳入同一個值。GPIO、能力、實體解析度、
SPI 時脈與 payload 尺寸只定義在
`esp32/ink-display-7C-photo/hardware_profile.h`；韌體不支援運行中改接 GPIO。

## 中央 BoardConfig

| 功能 | PhotoPainter GPIO／設定 |
|---|---|
| EPD | DC 8、CS 9、SCK 10、MOSI 11、RST 12、BUSY 13 |
| EPD SPI | FSPI 相容層、MODE0、初始 4 MHz |
| SD | CS 38、SCK 39、MISO 40、MOSI 41；獨立 SPI bus |
| I²C | SDA 47、SCL 48、400 kHz；裝置個別 probe |
| 按鍵 | BOOT 0、KEY 4 active-low、PWR 5 保留原廠電源用途 |
| 音訊 | MCLK 14、WS 16、BCLK 15、DIN 18、DOUT 17、PA 7 |
| 面板 | 800×480、4bpp、192,000 bytes、E6 六色 |
| MCU | ESP32-S3-WROOM-1-N16R8、16 MiB Flash、8 MiB OPI PSRAM |

板級資料已與 [Waveshare 官方 repository](https://github.com/waveshareteam/ESP32-S3-PhotoPainter)
的 `user_app.cpp`、`config.h`、display／PMIC 實作交叉核對，並參考
[0BSD 第三方同硬體實作](https://github.com/will-rigby/PhotoPainter-Nginx-Home-Assistant-Device)。
實際使用的授權聲明在
`esp32/ink-display-7C-photo/THIRD_PARTY_NOTICES.md`。

## 顯示、PSRAM 與方向

- InkTime 既有 server／Manifest 契約仍是直向 480×800；PhotoPainter adapter 在
  PSRAM 中一次轉成面板原生 800×480 row-major 4bpp，不更動 API 或既有發布檔。
- 原生 palette index 固定為黑 0、白 1、黃 2、紅 3、藍 5、綠 6；4、7 與其他
  index 轉為白色，避免送出未定義顏色。
- `rotation=0／180` 只在轉換層執行一次；沒有在圖片與傳輸層重複旋轉。
- 建置時強制 ESP32-S3、16 MiB Flash 與 OPI PSRAM 選項；啟動時再核對實體
  16 MiB Flash／8 MiB PSRAM。不存在或不足時不退回 internal SRAM，也不開始
  大型 framebuffer 流程。
- 所有 BUSY 等待上限 60 秒。官方現行程式顯示 BUSY low 代表忙碌，本 adapter
  以 active-low 為安全預設；逾時會停止傳輸、reset、盡力 power-off，且回報錯誤。
- 每次 full refresh 後明確執行 power-off 與 deep-sleep sequence；沒有宣稱快速刷新。

## SD、快取與斷電恢復

- SD 先以 20 MHz 初始化，失敗後只以 4 MHz重試一次；無 SD 時 Wi-Fi、下載、
  診斷與 RAM→面板流程仍可執行。
- 啟動建立 `/originals`、`/cache`、`/config`、`/logs`。
- PSRAM 與 SD 之間固定經 4,096-byte internal-RAM bounce buffer；逐 chunk 檢查
  read／write byte count，寫完 flush／close。
- 快取 header 驗證 magic、版本、800×480、4bpp、rotation、來源 hash、payload
  長度與 CRC32。損壞快取會刪除並重新下載。
- 寫入採同目錄 `.tmp`，舊檔先 rename 為 `.bak`，新檔再 rename 成正式檔；若中途
  斷電，下次啟動可恢復 `.bak`，不會把半寫入檔案當成有效畫面。

## I²C、PMIC、RTC 與感測器

- I²C 單一裝置失敗不會中止其他裝置。SHTC3 以 0x70 probe，量測後驗證兩段
  CRC-8 並送回 sleep；CRC 錯誤不回報溫濕度。
- PCF85063 以 0x51 probe，RTC 只保存 UTC。NTP 成功後寫入 RTC；NTP 失敗時可由
  RTC 恢復排程，時區仍使用 InkTime 裝置設定，不硬編碼在 RTC。
- PMIC 先在 0x34 讀取 chip ID；只有 ID 0x4A 才標為 AXP2101。實作只讀取 USB、
  電池電壓與 fuel-gauge 百分比，不修改 LDO、充電電流或 shutdown register。
- 無法識別的 revision 標為 `unknown`，仍允許 USB 診斷。已知 AXP2101 且沒有 USB
  時，電壓低於暫定 3,500 mV 不刷新；正式門檻必須依電池、面板與實測修訂。
- 本專案不需要音訊，因此不初始化 ES7210／ES8311；PA GPIO 7 維持 LOW。

## 按鍵、喚醒與網路邊界

- GPIO 4 有 debounce 且可作 EXT0 active-low wake。短按依 NVS 上次 index 顯示下一張；
  長按至少 1.2 秒會重抓目前圖片並略過 cache。睡前等待按鍵釋放以免重複喚醒。
- GPIO 5 完全不作一般輸出；GPIO 0 不取樣、不驅動，完整保留原廠 BOOT／下載用途。
- Wi-Fi、HTTP、NTP、AP 與 EPD 都有有限 timeout。已知電池模式 Wi-Fi 失敗後直接依
  RTC／備援排程睡眠，不無限重試。已確認 AXP2101 USB 供電時，設定 WebServer
  保持運作直到拔除 VBUS；未知 PMIC 只能保留 5 分鐘 AP 診斷窗口，以免誤把電池
  模式永久保持喚醒。
- Manifest 必須是有限 Content-Length 的 JSON；圖片必須是精確長度的
  `application/octet-stream` 且 SHA-256 相符。
- 尚未提供可信 CA 配置，因此預設會在建立連線前明確拒絕 HTTPS，不會進入 Arduino
  core 的 insecure TLS 路徑。只有明確編譯 `INKTIME_ALLOW_UNVERIFIED_HTTPS=1`
  才允許開發用例外；隔離 LAN 可使用 HTTP，跨網路應先使用 VPN／IoT VLAN 或加入
  CA provisioning。
- 韌體目前沒有 MQTT／Home Assistant client，因此沒有 Topic、Discovery entity 或
  callback 可遷移；既有 Bearer Token Manifest／Status API 保持不變。

## 能源遙測與續航儀表板

- 韌體 2.4.0 在低頻 Status API 回報電池電壓、估算百分比、USB 狀態、刷新耗時與
  從開機到狀態上傳前的完整喚醒週期耗時；既有 Profile 也會回報刷新與喚醒耗時。
- Web「能源」頁保存最近 400 天樣本，提供 7／30／90／365 天電量、電壓與刷新耗時
  SVG 曲線，不引入第三方圖表服務或外部 CDN。
- 續航同時顯示兩種算法：明確排除 USB 樣本的實際放電斜率，以及由電池容量、整板
  deep-sleep 待機電流、完整喚醒週期平均電流、每日刷新次數與安全保留量計算的模型。
- 電池容量與兩項電流必須由管理員在 Web 依電池規格／外接功率計量測值填入；系統
  不會把 ESP32-S3 datasheet 的 SoC deep-sleep 電流冒充成整板實測值。
- PMIC 百分比仍標示為估算；沒有 PhotoPainter 實機前，曲線與算法只代表軟體功能
  已驗證，不代表電池曲線、待機電流或續航數字已完成硬體校正。

## 編譯

先安裝 Arduino CLI 1.5.1、ESP32 core 3.3.10、GxEPD2 1.6.9、ArduinoJson 7.4.3。

```bash
# 既有 PCB（Release）
arduino-cli compile \
  --fqbn 'esp32:esp32:esp32s3' \
  esp32/ink-display-7C-photo

# Waveshare PhotoPainter（Release）
arduino-cli compile \
  --fqbn 'esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=app3M_fat9M_16MB,PSRAM=opi,CDCOnBoot=cdc' \
  --build-property 'compiler.cpp.extra_flags=-DDEVICE_PROFILE=DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER' \
  esp32/ink-display-7C-photo

# Waveshare PhotoPainter（Debug）
arduino-cli compile \
  --fqbn 'esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=app3M_fat9M_16MB,PSRAM=opi,CDCOnBoot=cdc,DebugLevel=debug' \
  --build-property 'compiler.cpp.extra_flags=-DDEVICE_PROFILE=DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER -DINKTIME_DEBUG_LOG=1' \
  esp32/ink-display-7C-photo
```

`app3M_fat9M_16MB` 提供 3 MiB 雙 OTA app slot 與約 9.9 MiB FAT partition；本韌體的
圖片快取使用外接 SD，不會自動使用 Flash FAT partition。OTA 尚未實作，但分割區先
保留 rollback 空間。

2026-07-19 在本機以 Arduino CLI 1.5.1、ESP32 core 3.3.10、GxEPD2 1.6.9、
ArduinoJson 7.4.3 完成以下編譯；這些是軟體建置結果，不是實機驗證：

| Profile | 模式 | 程式 Flash | 全域變數 |
|---|---:|---:|---:|
| 既有 GDEY | Release | 1,213,069／1,310,720 bytes（92%） | 96,564 bytes（29%） |
| 既有 GDEP | Release | 1,213,141／1,310,720 bytes（92%） | 96,564 bytes（29%） |
| 既有 GDEY | Debug | 1,289,201／1,310,720 bytes（98%） | 96,612 bytes（29%） |
| PhotoPainter | Release | 1,161,527／3,145,728 bytes（36%） | 49,168 bytes（15%） |
| PhotoPainter | Debug | 1,254,227／3,145,728 bytes（39%） | 49,296 bytes（15%） |

既有板 Debug 僅餘 21,519 bytes app 空間，僅適合短期診斷；PhotoPainter 的雙 OTA
slot 仍有足夠餘裕，但 OTA 簽章、rollback 與實際燒錄流程尚未實作。

## 實機 smoke test

請逐項記錄板 revision、韌體 commit、供電方式、Serial log、結果與量測值：

1. USB Serial 開機，確認 firmware、board profile 與 reset reason。
2. 確認 Flash 約 16 MiB、OPI PSRAM 約 8 MiB；移除 PSRAM 設定時必須明確停止刷新。
3. 記錄完整 I²C 掃描／probe 結果，單一裝置缺席時其他裝置仍工作。
4. 讀取 PMIC chip ID，確認是 AXP2101、TG28 或其他型號與實際板 revision。
5. FAT32 SD 以 20 MHz mount；製造首次失敗後只降到 4 MHz一次。
6. 移除 SD 開機，確認網路、診斷與 RAM 顯示不 crash。
7. SHTC3 正常讀值；注入 CRC 錯誤時不得回報溫濕度，量測後回 sleep。
8. NTP 寫入 RTC；斷網與 deep sleep 後由 RTC 恢復正確本地排程。
9. 顯示黑、白、黃、紅、藍、綠六張純色，確認 palette code。
10. 顯示六色色條與 1-pixel 棋盤格，檢查 nibble packing／邊界。
11. 用非對稱測試圖確認 0°／180°；不可出現雙重旋轉或鏡射。
12. 正常 full refresh 記錄 BUSY level、時間與溫度；不可宣稱快速刷新。
13. 固定 BUSY 在 active level，確認 60 秒內 timeout、無 watchdog reset、面板安全關閉。
14. 邏輯分析儀確認 EPD MODE0 約 4 MHz，SD 與 EPD 位於不同 SPI host／GPIO。
15. 刷新後量測面板 power-off／deep-sleep 與整板待機電流。
16. GPIO 4 短按顯示下一張、長按略過 cache，按住喚醒後不連續重觸發。
17. GPIO 4 EXT0 與 RTC timer 均可喚醒；GPIO 5 全程沒有被韌體拉高／拉低。
18. 已確認 USB 模式可完成配網、診斷且不睡眠；拔除 VBUS 或電池模式 Wi-Fi 逾時後
    會停止服務並睡眠。
19. 下載、cache write、rename 各階段斷電，重啟後只能使用完整正式檔或 `.bak`。
20. 低於暫定門檻時禁止刷新；USB 接入時可診斷，並據實校正安全門檻。

## 尚待實機決定

1. 實際板 revision 與 PMIC 型號。
2. BUSY active-low 是否適用該批次與實際 waveform。
3. GDEP073E01 init／power sequence 是否與出貨面板完全相同。
4. 實際安裝方向與 0°／180°對應。
5. GPIO 5 在該 revision 的完整電源控制行為。
6. 3,500 mV 低電量門檻及 battery percentage 是否只標示估算。
7. PCF85063 是否存在，以及斷電保存時間。
8. 是否需要裝置端 JPEG／progressive JPEG；目前 server 已輸出 raw indexed payload。
9. 是否需要 RTC、音訊或小智 AI；目前 RTC adapter 啟用、音訊與小智停用。
10. 16 MiB 分割區、未來 OTA 簽章與 rollback 的實際容量。
11. 是否要求新增 MQTT／Home Assistant entity；目前只保留既有 HTTP API。
12. 無 SD 時從 RAM 顯示的連續穩定性與峰值記憶體。
13. 面板 driver／waveform 與出貨批次的授權及供應商版本。
14. 長期電池、弱 Wi-Fi、低溫與重複 full-refresh 壽命。
