# Waveshare ESP32-S3-PhotoPainter 支援與安全基準

維護或實板除錯前，必須先讀
[PhotoPainter Rev2.0 TG28 實板除錯交接紀錄](PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md)；
該文件保存 2026-08-22 至 2026-08-23 的官方／InkTime A/B、ALDO4 根因、安全禁區與
尚未完成的實板驗收，避免後續維護者重做已證偽或有風險的 PMIC 實驗。

## 支援狀態

InkTime 2.6.0 可用單一 compile-time Profile 切換既有 PCB 與 Waveshare
ESP32-S3-PhotoPainter。使用者實際板與 Waveshare Rev2.0 原理圖都確認 PMIC 為 TG28；
這仍不代表 GPIO 喚醒、面板、SD、電池或睡眠電流已完成 InkTime 實機驗證。

```cpp
#define DEVICE_PROFILE DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER
```

Arduino CLI 以 `compiler.cpp.extra_flags` 傳入同一個值。EPD、SD、I²C、按鍵、音訊、
能力、實體解析度、SPI 時脈與 payload 尺寸集中定義在
`esp32/ink-display-7C-photo/hardware_profile.h`；PWR／ACT indicator 目前仍是
`photopainter_support.cpp` 的 board-specific constant，已在 Rev2.0 交接紀錄列為後續應
補上的 compile-time contract。韌體不支援運行中改接 GPIO。

## Stock-first 與 Enhanced 邊界

本專案把 PhotoPainter 分成兩條可驗證的路徑：

- `stock_compat`：保留原廠韌體、不刷機；InkTime 在 Server 交付邊界把既有直向
  Production BIN 轉為 Stock `/dataUP` 的 mode byte + 24-bit BMP，固定為
  `1,152,055` bytes。原廠 HTTP 回應只代表 upload accepted，不能推導電子紙已完成刷新。
- `inktime_offline_schedule`：只有明確刷入並設定 Enhanced 韌體才啟用多時間離線排程。
  Server 以每個 Slot 一個 Release／Queue Item 管理，韌體把通過 SHA-256、尺寸、CRC
  與 rotation 驗證的 native frame 原子寫入 `/inktime/frames/<sha256>-r*.itf`。

Stock 原始碼交叉核對固定在官方 repository commit
[`a5e8f757ba0cafbb5586f07d3e83bda3184c0845`](https://github.com/waveshareteam/ESP32-S3-PhotoPainter/commit/a5e8f757ba0cafbb5586f07d3e83bda3184c0845)。
該版本的 Mode 1 設定檔與圖片目錄是
`/sdcard/06_user_Foundation_img/config.txt` 與 `/sdcard/06_user_foundation_img`；
Stock 原始碼使用相對秒數 timer，不足以證明支援 InkTime 的任意每日時間清單，因此
`STOCK_CUSTOM_TIME_LIST = NOT SUPPORTED/UNVERIFIED`。精準不等間隔時間請使用 Enhanced。

## 中央 BoardConfig

| 功能 | PhotoPainter GPIO／設定 |
|---|---|
| EPD | DC 8、CS 9、SCK 10、MOSI 11、RST 12、BUSY 13 |
| EPD SPI | SPI3、ESP-IDF half-duplex、手動 CS、純寫入 MODE0、實板驗證原廠 factory 10 MHz |
| SD | CS 38、SCK 39、MISO 40、MOSI 41；獨立 SPI bus |
| I²C | SDA 47、SCL 48、100 kHz；共用總線採官方裝置設定中的保守速率，裝置個別 probe |
| 按鍵 | BOOT 0、KEY 4 active-low、PWR 5 保留原廠電源用途 |
| 指示燈 | PWR 紅燈 GPIO 45、ACT 綠燈 GPIO 42，兩者 active-low；不等同 GPIO 5 PWR 按鍵 |
| 音訊 | MCLK 14、WS 16、BCLK 15、DIN 18、DOUT 17、PA 7 |
| 面板 | 800×480、4bpp、192,000 bytes、E6 六色 |
| MCU | ESP32-S3-WROOM-1-N16R8、16 MiB Flash、8 MiB OPI PSRAM |

板級資料已與 [Waveshare 官方 repository](https://github.com/waveshareteam/ESP32-S3-PhotoPainter)
的固定版本 [`a5e8f757`](https://github.com/waveshareteam/ESP32-S3-PhotoPainter/commit/a5e8f757ba0cafbb5586f07d3e83bda3184c0845)
之 `user_app.cpp`、`config.h`、display／PMIC 實作交叉核對。實際使用的授權聲明在
`esp32/ink-display-7C-photo/THIRD_PARTY_NOTICES.md`。

## 顯示、PSRAM 與方向

- InkTime 既有 server／Manifest 契約仍是直向 480×800；PhotoPainter adapter 在
  PSRAM 中一次轉成面板原生 800×480 row-major 4bpp，不更動 API 或既有發布檔。
- InkTime wire palette 固定為黑 0、白 1、綠 2、藍 3、紅 4、黃 5；adapter
  會轉成 E6 面板原生 index。任何其他 wire index 都會拒絕整張 frame 並保留舊畫面，
  不會把未知值悄悄改成白色。
- `rotation=0／180` 只在轉換層執行一次；沒有在圖片與傳輸層重複旋轉。
- 建置時強制 ESP32-S3、16 MiB Flash 與 OPI PSRAM 選項；啟動時再核對實體
  16 MiB Flash／8 MiB PSRAM。不存在或不足時不退回 internal SRAM，也不開始
  大型 framebuffer 流程。
- 所有 BUSY 等待上限 60 秒。官方現行程式顯示 BUSY low 代表忙碌，本 adapter
  以 active-low 為安全預設。實板確認 `POWER_ON` 可能在 MCU 第一次取樣前完成，因此
  該階段沿用官方「等待回到 ready」行為；真正的 `DISPLAY_REFRESH` 必須先觀察到 BUSY
  拉低、再回到高電位，避免面板未供電或命令未送達時被上拉電位誤判成功。未拉低或
  逾時都會停止傳輸、reset、盡力 power-off，且回報錯誤。
- 每次 full refresh 完全沿用官方 `POWER_ON`、第二段 booster、`DISPLAY_REFRESH`、
  `POWER_OFF` 與 BUSY 完成順序。官方 PhotoPainter driver 未使用的額外面板 sleep command
  或 GPIO pulldown／hold 不會加入；沒有宣稱快速刷新。

## SD、快取與斷電恢復

- SD 先以 20 MHz 初始化，失敗後只以 4 MHz重試一次；無 SD 時 Wi-Fi、下載、
  診斷與 RAM→面板流程仍可執行。
- 啟動建立 `/originals`、`/cache`、`/config`、`/logs`，以及 Enhanced 使用的
  `/inktime/schedule`、`/inktime/frames`、`/inktime/journal`、`/inktime/state`。
- PSRAM 與 SD 之間固定經 4,096-byte internal-RAM bounce buffer；逐 chunk 檢查
  read／write byte count，寫完 flush／close。
- `/cache` 是可重建的 derived cache；其 header 驗證 magic、版本、800×480、4bpp、
  rotation、來源 hash、payload 長度與 CRC32。Enhanced 正式內容另使用完整 SHA-256
  檔名與 `ITF2` header，不把 32-bit cache key 當成內容身份。損壞檔案會刪除並保留
  舊的可用檔案，不會把半寫入內容當成畫面。
- 寫入採同目錄 `.tmp`，舊檔先 rename 為 `.bak`，新檔再 rename 成正式檔；若中途
  斷電，下次啟動可恢復 `.bak`，不會把半寫入檔案當成有效畫面。

## I²C、PMIC、RTC 與感測器

- I²C 單一裝置失敗不會中止其他裝置。SHTC3 以 0x70 probe，量測後驗證兩段
  CRC-8 並送回 sleep；CRC 錯誤不回報溫濕度。
- 開機與有界重試前先釋放 SDA／SCL；若 reset 中斷 transaction，最多以 open-drain
  SCL 送九個 clock 再送 STOP。程式永遠不主動驅動 I²C 高電位，兩線仍為 low 時立即
  fail-closed，等待完整斷電恢復，不繼續寫 PMIC 或驅動 EPD。
- 若 SDA／SCL 持續為 low，依 Waveshare 官方電源流程：拔除 USB、長按 PWR 5 至 6 秒
  直到 PWR 指示燈熄滅、等待至少 10 秒，再接回 USB 並短按 PWR 開機。單純拔插 USB
  或 ESP-only reset 不一定會清除 PMIC／共享 I²C 的鎖定狀態；不要以 GPIO 強驅兩線為高。
- PCF85063 以 0x51 probe，RTC 只保存 UTC。NTP 成功後寫入 RTC；NTP 失敗時可由
  RTC 恢復排程，時區仍使用 InkTime 裝置設定，不硬編碼在 RTC。
- Waveshare Rev2.0 原理圖確認 UP1 是 TG28、I²C 位址 0x34，且 **ALDO4 直接供應
  `EPD_VCC`**。TG28 資料表沒有定義 AXP2101 專用的 0x03 identity 契約，因此 Rev2.0
  driver 以 0x34 probe 及 TG28 的 0x90／0x95
  可讀性作 fail-closed 相容性檢查。
- 每次顯示前只以 read-modify-write 將 TG28 `REG95[4:0]` 設為 `0x1C`（3.3 V），再將
  `REG90[3]` 設為 1 啟用 ALDO4；兩步都必須讀回一致。韌體會像官方全域
  `ePaperPort` constructor 一樣，在 `setup()` 前啟動並保留 SPI3 pin matrix，只設定獨立的
  CS high、DC low、RESET high 與 BUSY pull-up；SCK／MOSI 建立 SPI3 後不得再當一般 GPIO
  重新設定。若 ALDO4 原本已開啟只等待
  10 ms，真正從關閉轉為開啟時則在 RESET high 下等待 500 ms，再初始化 EPD。此路徑
  不接觸 GPIO5 PWR 或 GPIO21 PMIC IRQ。
- 冷啟動初始化的 `0xAA` 六個參數逐 byte 傳送，每個 byte 都各自完成一次 CS low／high
  framing，與實板驗證成功的官方 factory driver 相同。
  EPD transport 固定使用已在 Rev2.0 實板完成純白刷新的原廠 factory 10 MHz，並在
  `EPD_Init` 完成後等待 3 秒才送第一個 framebuffer；不使用僅由另一個官方 runtime
  原始碼推得、但尚未在此板冷啟動驗證的 40 MHz。
  Framebuffer 以原廠的 5000-byte polling transaction 連續送出；CS low 期間不主動
  `yield()`，控制腳使用 ESP-IDF GPIO，不讓 Arduino pin ownership 再次改寫已建立的
  SPI3／CS 時序。
  顯示 controller 完成 `POWER_OFF`／SPI shutdown 後維持官方 runtime 的 ALDO4 狀態；
  實板曾在清除該 rail 後的 ESP-only reset 觀察到共享 I²C 線持續為 low，完整斷電前
  無法再由 ESP 存取 PMIC，因此不在一般 refresh／deep-sleep 路徑關閉它。任一步失敗
  都不送出電子紙更新命令。
- 韌體不寫 TG28 的 DCDC、充電、全機 shutdown、fast-power-on 或其他 LDO register。
  status、VBAT 與 fuel-gauge register 僅供遙測，也不作低電壓刷新門檻。
- 本專案不需要音訊，因此不初始化 ES7210／ES8311；PA GPIO 7 維持 LOW。

## 按鍵、喚醒與網路邊界

- GPIO 4 有 debounce，並使用 EXT1 `ANY_LOW` active-low wake；只有 EXT1 wake-status
  mask 確實包含 GPIO 4 才視為 USER／KEY 喚醒。短按保留既有 USER 動作；持續至少
  1.2 秒但未滿 4 秒要求 bounded forced network refresh；刻意持續至少 4 秒才授權
  recovery/service。timer wake 仍獨立啟用，Enhanced timer wake 的本地排程只讀正式
  Frame，不呼叫 Wi-Fi、NTP 或 HTTP。睡前等待按鍵釋放以免重複喚醒。
- GPIO 5 完全不作一般輸出；GPIO 0 不取樣、不驅動，完整保留原廠 BOOT／下載用途。
- 開機後 PWR 紅燈維持亮起，電子紙傳輸期間 ACT 綠燈亮起；進入 deep sleep 前兩燈
  都會熄滅。燈號是狀態提示，不取代 BUSY cycle 與實際面板變化的刷新判定。
- Wi-Fi、HTTP、NTP、AP 與 EPD 都有有限 timeout。Wi-Fi 失敗時先嘗試由 RTC 與正式
  快取完成到期的離線 Slot；否則進入有界設定入口。PMIC 辨識與電池讀值不參與這個
  決策，因此讀不到電源資訊時仍能看見並修正網路或設定問題。
- 持續至少 4 秒的 GPIO 4 喚醒才是 PhotoPainter 的明確實體 recovery/service 授權；
  USB 供電本身與 1.2 至未滿 4 秒的 forced refresh 都不授權設定變更。電源來源確認為
  USB 時可沿用長時間 service；PMIC 無法確認時仍可進入 recovery，但不解除
  max-awake supervisor，且設定服務最多五分鐘。
- Manifest 必須是有限 Content-Length 的 JSON；圖片必須是精確長度的
  `application/octet-stream` 且 SHA-256 相符。
- Backend transport 預設只接受有 compile-time 或 AP portal provisioning trust anchor
  的 HTTPS；沒有 CA 會在建立連線前明確拒絕，不會進入 Arduino core 的 insecure TLS
  路徑，也沒有 `setInsecure()` fallback。HTTP 只有在明確編譯
  `INKTIME_ALLOW_INSECURE_DEVICE_HTTP=1` 且目標是私有 LAN host 時才允許；跨網路應
  使用 HTTPS、VPN／IoT VLAN 與可驗證的 CA。首次配網的 AP SSID、隨機密碼與 URL 會
  顯示在 portal 與 PhotoPainter 配對畫面，不會把密碼寫進 Serial log。
- CA provisioning 的 build、portal 欄位、錯誤碼與人工驗收步驟見
  [ESP32 TLS／配網信任根配置](ESP32_TLS_PROVISIONING_ZH_TW.md)。
- 韌體目前沒有 MQTT／Home Assistant client，因此沒有 Topic、Discovery entity 或
  callback 可遷移；既有 Bearer Token Manifest／Status API 保持不變。

## 自動能源遙測

- 韌體 2.6.0 在低頻 Status API 回報可取得的電池電壓、估算百分比、USB 狀態、刷新耗時與
  從開機到狀態上傳前的完整喚醒週期耗時；既有 Profile 也會回報刷新與喚醒耗時。
- Web「能源」頁保存最近 400 天樣本，提供 7／30／90／365 天電量、電壓、刷新耗時、
  完整喚醒時間與最近樣本；所有資料都由裝置自動回報。
- 頁面不再提供電池容量、待機電流、喚醒平均電流或安全保留量表單，也不計算依賴
  人工量測的續航模型。TG28 遙測只協助診斷，不會控制裝置是否可用；續航與睡眠電流
  仍須以實板量測，不能由 register 讀值推導為已通過。

## 編譯

先安裝 Arduino CLI 1.5.1、ESP32 core 3.3.10、GxEPD2 1.6.9、ArduinoJson 7.4.3。

本專案必須使用 repository-owned partition table；stock `app3M_fat9M_16MB` 的 20 KiB
NVS 無法容納 32-entry ACK journal 的 COW／migration peak。Arduino-ESP32 支援在
sketch 目錄使用 `partitions.csv`，因此每次編譯前先把對應 CSV 複製成該檔名：
表內固定保留 pinned Arduino-ESP32 upload recipe 使用的 `otadata=0xE000` 與
`app0=0x10000`，512 KiB NVS 位於兩個 OTA slot 之後；不可把 NVS 擴張到
`0x10000` app upload range 內。

```bash
cp esp32/ink-display-7C-photo/inktime_default_4M.csv \
  esp32/ink-display-7C-photo/partitions.csv
```

```bash
# 既有 PCB（Release）
arduino-cli compile \
  --fqbn 'esp32:esp32:esp32s3:FlashSize=4M' \
  --build-property 'upload.maximum_size=1441792' \
  esp32/ink-display-7C-photo

# Waveshare PhotoPainter（Release）
cp esp32/ink-display-7C-photo/inktime_photopainter_3M_16MB.csv \
  esp32/ink-display-7C-photo/partitions.csv
arduino-cli compile \
  --fqbn 'esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,CDCOnBoot=cdc' \
  --build-property 'upload.maximum_size=3145728' \
  --build-property 'compiler.cpp.extra_flags=-DDEVICE_PROFILE=DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER' \
  esp32/ink-display-7C-photo

# Waveshare PhotoPainter（Debug）
arduino-cli compile \
  --fqbn 'esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,CDCOnBoot=cdc,DebugLevel=debug' \
  --build-property 'upload.maximum_size=3145728' \
  --build-property 'compiler.cpp.extra_flags=-DDEVICE_PROFILE=DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER -DINKTIME_DEBUG_LOG=1' \
  esp32/ink-display-7C-photo
```

完成 PhotoPainter 編譯後移除 sketch-local partition override：

```bash
rm -f esp32/ink-display-7C-photo/partitions.csv
```

`inktime_photopainter_3M_16MB` 提供 512 KiB NVS、3 MiB 雙 OTA app slot 與約 9.4 MiB
FAT partition；本韌體的
圖片快取使用外接 SD，不會自動使用 Flash FAT partition。OTA 尚未實作，但分割區先
保留 rollback 空間。

PhotoPainter 的 `CDCOnBoot=cdc` 使用 ESP32-S3 原生 USB CDC／JTAG 作為正式與除錯
生命週期 Log；不要另建 `HardwareSerial(0)`，否則 Type-C 埠可燒錄但看不到應用程式
開機紀錄。既有未啟用 USB CDC 的 ESP32-S3 Profile 中，Arduino `Serial` 仍映射 UART0。

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

## 不需量測儀器的使用確認

燒錄前由 Hosted CI 編譯固定的 PhotoPainter Profile；燒錄後只需確認一般使用結果：

1. 開機畫面顯示配對資訊，且 Web 設定入口可連線。
2. 成功下載並顯示一張六色正式圖片，方向正確。
3. Wi-Fi 或 PMIC 資訊缺失時仍可進入設定／診斷，不會因電源讀值未知而永久停止刷新。
4. 排程後裝置能自行休眠並在下一個時間再次運作；GPIO 4 可要求本地下一張／網路更新。
5. 若頁面回報 BUSY timeout、記憶體或儲存錯誤，保留舊畫面並從 Web 錯誤資訊處理，
   不需用電流表、萬用電表或邏輯分析儀判斷。

2026-08-22 在 Waveshare Rev2.0 實板完成下列硬體 A/B：完整恢復燒錄前的官方 16 MB
備份後，以 BOOT 雙擊成功刷新官方電池資訊畫面，確認 TG28、EPD_VCC、SPI、BUSY 與
面板可用；再寫入 InkTime TG28 安全版後，裝置 log 回報 `pairing_display_ready`，配對
畫面實際完成刷新，耗時 `30090 ms`。這是配對畫面的實板通過，不等同正式六色照片、
排程喚醒、睡眠電流或電池續航已完成驗收。

2026-08-23 依 Rev2.0 原理圖與 TG28 資料表將實際 `EPD_VCC` 電源軌修正為 ALDO4
（`REG95[4:0]`／`REG90[3]`）後，再以安裝電池、拔除 USB、完整關機等待後重新上電的
方式驗證；InkTime 配對畫面確實再次刷新，確認 ALDO4 修正通過真正 PMIC 冷啟動。
USB CDC 在斷電期間中斷且重新接線後恢復，因此未擷取到發生於 USB 尚未連線時的早期
boot log；本次通過依據是實體面板變化，不延伸宣稱正式照片、排程喚醒或睡眠電流已通過。

同日再燒錄 PWR／ACT 指示燈版（app SHA-256
`187cd069b554f3554248ab58c77f9e8ae4ed4028e797905c113a5652b467573a`）：暖啟動時
log 回報 `pairing_display_ready`，耗時 `30101 ms`，實體面板與 ACT 燈均有變化；接著
拔除 USB 讓裝置進入 deep sleep，短按 KEY1 後配對畫面再次刷新。這項結果通過 GPIO4
EXT1 按鍵喚醒及睡眠後再次刷新，不等同尚未執行的 timer 排程喚醒或睡眠電流量測。

本輪的安全結論來自官方 commit `a5e8f757…` 原始碼比對、compile-time 腳位鎖定與
Hosted CI 編譯，不是對實體面板壽命或電池續航的保證；這項限制不會轉成使用者必須
執行的電流／電壓驗收工作。
