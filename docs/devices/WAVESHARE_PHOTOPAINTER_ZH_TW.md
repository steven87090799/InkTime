# Waveshare ESP32-S3-PhotoPainter 支援與安全基準

維護或實板除錯前，必須先讀
[PhotoPainter Rev2.0 TG28 實板除錯交接紀錄](PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md)；
該文件保存 2026-08-22 至 2026-08-23 的官方／InkTime A/B、ALDO4 根因、安全禁區與
尚未完成的實板驗收，避免後續維護者重做已證偽或有風險的 PMIC 實驗。

## 支援狀態

InkTime 韌體 2.8.6 可用單一 compile-time Profile 切換既有 PCB 與 Waveshare
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
- 韌體不寫 TG28 的 DCDC、充電、全機 shutdown 或 fast-power-on；除了原有 ALDO4
  顯示控制，只增加下述 ALDO3 未使用音訊關閉。
  status、VBAT 與 fuel-gauge register 僅供遙測，也不作低電壓刷新門檻。
- 本專案不需要音訊，因此不初始化 ES7210／ES8311；PA GPIO 7 維持 LOW，I²S
  GPIO14～18 設為 input；確認 TG28 可讀後只清除 `REG90[2]`，關閉 ALDO3／Audio_VCC。

## 按鍵、喚醒與網路邊界

- GPIO 4 有 debounce，並使用 EXT1 `ANY_LOW` active-low wake；只有 EXT1 wake-status
  mask 確實包含 GPIO 4 才視為 USER／KEY 喚醒。短按保留既有 USER 動作；持續至少
  1.2 秒但未滿 4 秒要求 bounded forced network refresh；刻意持續至少 4 秒才授權
  recovery/service。timer wake 仍獨立啟用，Enhanced timer wake 的本地排程只讀正式
  Frame，不呼叫 Wi-Fi、NTP 或 HTTP。睡前等待按鍵釋放以免重複喚醒。
- 未完成配對而停留在五分鐘 Portal 時，韌體也會以 active-low、35 ms debounce 持續
  讀取 GPIO 4；單擊會重畫同一組 SSID／AP 密碼／設定網址，頁尾以 `KEY REFRESH n`
  顯示本次動作；450 ms 內雙擊則顯示 TG28 唯讀取得的電量百分比、電池電壓、USB
  供電、是否充電與充電階段。從 deep sleep 由第一次 KEY 喚醒後，在同一時間窗第二次
  點擊也會直接顯示電源頁。電源頁完成刷新後保留 30 秒，再從 SD 讀取並驗證最後成功
  Frame，無網路刷回原照片；若本地 Frame 不存在或完整性失敗，才回到正常網路刷新。
  未配對時則恢復同一組 Portal 配對頁。因 E6 每次 full refresh 約 30 秒，從雙擊到原圖
  完全恢復通常約 90 秒；這段是使用者明確要求的有界醒著時間，仍受 10 分鐘 max-awake
  保護。按鍵不會重啟 SoftAP、輪替密碼、重設 Portal 起始時間、清除 NVS 或驅動
  GPIO 0／5／21；尚未配對時沒有正式照片可切換，完成配對後才恢復下一張／網路更新流程。
- 原廠以 BOOT 雙擊顯示電池頁；InkTime 為完整保留 GPIO 0 的下載模式，不在 runtime
  取樣 BOOT，而將相同唯讀功能放在 KEY1 雙擊。TG28 `REG00`、`REG01`、`REG34/35` 與
  `REGA4` 只讀取，不照抄原廠對 `REG17` 的 fuel-gauge 寫入，也不以 AXP2101 identity
  判斷 Rev2.0。電量百分比仍是 PMIC 估算值，不是容量校準證據。
- GPIO 5 完全不作一般輸出；GPIO 0 不取樣、不驅動，完整保留原廠 BOOT／下載用途。
- 開機後 PWR 紅燈維持亮起，電子紙傳輸期間 ACT 綠燈亮起；進入 deep sleep 前兩燈
  都會熄滅。燈號是狀態提示，不取代 BUSY cycle 與實際面板變化的刷新判定。
- Wi-Fi、HTTP、NTP、AP 與 EPD 都有有限 timeout。Wi-Fi 失敗時先嘗試由 RTC 與正式
  快取完成到期的離線 Slot；已配對的自動 timer wake 接著依排程／恢復策略睡眠，
  其他喚醒則保留有界設定入口。PMIC 辨識與電池讀值不參與這個決策，因此讀不到
  電源資訊時仍能以手動喚醒進入設定／診斷。
- PhotoPainter 的 10 分鐘 max-awake supervisor 以 RTC no-init memory 記錄連續 timeout，
  不會每次喚醒寫 NVS。前兩次 timeout 仍以 restart 嘗試恢復；第三次後不再持續 boot loop，
  而是保留 GPIO4 與 timer wake、關閉網路／LED，按 `1h → 6h → 24h → 每日一次` 退避；每次只允許一次 probation。正常進入 sleep、
  完整斷電或明確 GPIO4 recovery／factory reset 會清除計數。這是耗電失控保護；實板
  persistent-fault、timer wake 與睡眠電流驗收仍是 `NOT RUN`。
- 持續至少 4 秒的 GPIO 4 喚醒才是 PhotoPainter 的明確實體 recovery/service 授權；
  USB 供電本身與 1.2 至未滿 4 秒的 forced refresh 都不授權設定變更。電源來源確認為
  USB 時可沿用長時間 service；PMIC 無法確認時仍可進入 recovery，但不解除
  max-awake supervisor，且設定服務最多五分鐘。
- Manifest 必須是有限 Content-Length 的 JSON；圖片必須是精確長度的
  `application/octet-stream` 且 SHA-256 相符。
- Backend transport 同時支援嚴格的 RFC1918 literal IPv4 HTTP 與有 compile-time／Portal
  trust anchor 的 HTTPS；公開 IP、hostname、loopback、link-local、IPv6 HTTP 都拒絕。
  HTTPS 沒有 CA 會在建立連線前明確拒絕，不會進入 Arduino core 的 insecure TLS 路徑，
  也沒有 `setInsecure()` fallback。首次配網的 8 位隨機數字 AP 密碼會一致用於 SoftAP、
  Portal 與 PhotoPainter 配對畫面，不會寫進 Serial log。
- CA provisioning 的 build、portal 欄位、錯誤碼與人工驗收步驟見
  [ESP32 TLS／配網信任根配置](ESP32_TLS_PROVISIONING_ZH_TW.md)。
- 韌體目前沒有 MQTT／Home Assistant client，因此沒有 Topic、Discovery entity 或
  callback 可遷移；既有 Bearer Token Manifest／Status API 保持不變。

## 耗電異常恢復與診斷

- 已配對 PhotoPainter 的 timer wake 在 Wi-Fi 失敗時，先保留到期的本地正式照片
  fallback，再直接依既有排程／恢復策略睡眠，不自動開啟五分鐘設定熱點。未配對、
  冷啟動與 KEY 手動喚醒仍保留原設定入口；長按 KEY 的明確 recovery 不受此分支攔截。
  這不新增每 15 分鐘連網；有可靠時間時沿用原本的排程選擇。
- 離線模式時間未知、或 schedule transaction 受阻時，改為 `15m → 30m → 60m → 每小時`
  重試。獨立 `dashcfg/pwr_retry` 只在進度變更時寫入，飽和後不反覆寫 NVS；時間與交易
  恢復、進入有效離線排程計算時清除。NVS 開啟／寫入失敗則該輪保守睡一小時。
  不改 RTC memory 保留策略；factory reset 同時清除此 key。正常到期照片與既有
  有時間戳的 schedule retry 繼續使用原流程。
- 睡前序列事件 `sleep_diagnostics` 回報 `awake_ms`、實際選定的 `sleep_seconds`
  與 `wake_cause`；醒著時間計算至睡前紀錄處，不包括該紀錄自身輸出時間。
  `sleep_pmic_rails` 唯讀回報 REG90、ALDO3／ALDO4 enable bit；讀取失敗明確記為
  unknown，不寫入其他電源軌。enable bit 不代表耗電量，睡前 log 也不等於已量到
  deep-sleep 電流。
- 這些紀錄不增加連網、不保存到能源頁，也未補齊離線／刷新後的延後 Status 上傳。
  音訊關閉只在啟動階段執行，睡前仍只讀 PMIC；不關閉 ALDO4，不改 GPIO0／5／21、
  充電、分割區或面板命令。
  實際待機電流與每日電量改善仍須新版韌體實機比較，不能由程式或 CI 宣稱已改善。

## 2026-09-06 官方省電流程與音訊／SD 修正

本節重新取得官方 main，固定為 commit
[`a5e8f757ba0cafbb5586f07d3e83bda3184c0845`](https://github.com/waveshareteam/ESP32-S3-PhotoPainter/tree/a5e8f757ba0cafbb5586f07d3e83bda3184c0845)，
並檢查中文 Wiki 的 [Rev2.0 原理圖](https://www.waveshare.net/w/upload/a/ae/ESP32-S3-PhotoPainter-Schematic-v2.0.pdf)。
原理圖第 1 頁 UP1 pin 16 ALDO3 接 Audio_VCC，pin 19 ALDO2 未接，pin 15 ALDO4
接 EPD_VCC。**先前交接文件將音訊誤寫為 ALDO2；應以本次接線更正為準。**

官方有兩種不同的睡眠流程：

- [一般照片模式 Basic_mode.cpp](https://github.com/waveshareteam/ESP32-S3-PhotoPainter/blob/a5e8f757ba0cafbb5586f07d3e83bda3184c0845/01_Example/xiaozhi-esp32/components/user_app_bsp/mode_src/Basic_mode.cpp#L52)：
  啟用 GPIO0／4 EXT1 與 ESP timer，再進入 ESP deep sleep；兩處
  `axp_basic_sleep_start()` 都被註解掉。因此官方功能程式本身不能證明已使用下述
  PMIC 關電策略或達到 Wiki 的待機規格。
- [功耗測試 GoSLeep](https://github.com/waveshareteam/ESP32-S3-PhotoPainter/blob/a5e8f757ba0cafbb5586f07d3e83bda3184c0845/04_PowerConsumptionTest/01_Arduino_Src/01_Fac_Test/01_Fac_Test.ino#L25)
  先清除所有 wake source，只設 GPIO0 EXT1，沒有 timer；接著
  [axp_basic_sleep_start](https://github.com/waveshareteam/ESP32-S3-PhotoPainter/blob/a5e8f757ba0cafbb5586f07d3e83bda3184c0845/04_PowerConsumptionTest/01_Arduino_Src/01_Fac_Test/bsp_fac.h#L221)
  停用／清除 IRQ、設定 REG26 wake/sleep、停用電池電壓量測／偵測，關閉 DC2～5、
  ALDO1～4、BLDO1／2、CPUSLDO、DLDO1／2；沒有關閉 DC1。測試還把 GPIO21 當輸出
  拉動，InkTime 不採用該行為，也不搬用整組 PMIC sleep 或電池偵測修改。

與省電相關的 GPIO 定義，以原理圖及官方
[config.h](https://github.com/waveshareteam/ESP32-S3-PhotoPainter/blob/a5e8f757ba0cafbb5586f07d3e83bda3184c0845/01_Example/xiaozhi-esp32/main/boards/waveshare-s3-PhotoPainter/config.h)
和 [bsp_config.h](https://github.com/waveshareteam/ESP32-S3-PhotoPainter/blob/a5e8f757ba0cafbb5586f07d3e83bda3184c0845/04_PowerConsumptionTest/01_Arduino_Src/01_Fac_Test/bsp_config.h) 為準：

| GPIO | 功能 | InkTime 處理 |
|---|---|---|
| 0／4／5 | BOOT／KEY1／PWR 狀態 | 保留 BOOT、KEY1 EXT1 喚醒，PWR 不作輸出 |
| 1／2 | SD D1／D2，有外部 pull-up | SPI 模式未使用，不拉低 |
| 3 | PMIC CHGLED | 不當一般 LED 驅動 |
| 6 | RTC_INT，連到 PWRON 網路 | 不改接、不啟用新的全機斷電喚醒 |
| 7 | 喇叭功放 AudioCTR | LOW；不是麥克風電源開關 |
| 8／9／10／11／12／13 | EPD DC／CS／SCK／MOSI／RST／BUSY | 維持已驗證刷新流程 |
| 14／15／16／17／18 | I²S MCLK／BCLK／WS／DOUT／DIN | 無音訊，input、不輸出時鐘 |
| 19／20 | 原生 USB D−／D+ | 保留燒錄與除錯 |
| 21 | TG28 IRQ | 不驅動 |
| 38／39／40／41 | SD CS／CLK／MISO／MOSI | 睡前結束 SD/SPI，再 input 並關內部 pulls |
| 42／45 | ACT 綠燈／PWR 紅燈，active-low | 睡前 HIGH 關燈 |
| 43／44 | UART0 | 不作額外板級控制 |
| 47／48 | 共用 I²C SDA／SCL | 只用 open-drain，不強拉高 |

本次依使用者明確要求關閉未使用音訊，增加 `photopainter_audio_power.h`。官方
[ALDO3 driver](https://github.com/waveshareteam/ESP32-S3-PhotoPainter/blob/a5e8f757ba0cafbb5586f07d3e83bda3184c0845/01_Example/xiaozhi-esp32/components/pmicpower/src/XPowersAXP2101.tpp#L1845)
以 REG90 bit 2 控制此 rail；沿用官方命名不代表把 Rev2.0 PMIC 改判為 AXP2101。
啟動時 PA LOW、I²S input 後，讀 REG90，只清 bit 2 並完整讀回比對；已關閉不寫。
只允許一次不重播的寫入，失敗／讀回不符標記 PMIC unknown 並跳過本輪後續的
sensor/RTC 初始化，同時拒絕 EPD 供電命令，不猜測性寫回整組電源狀態。這也會關閉
共用 Audio_VCC 的播放 codec，未來若要恢復音訊必須重新設計上電／codec 初始化。
ALDO4、DCDC1、充電和全機 sleep registers 不變。

SD 的 TF1 pin 4 直接接 DCDC1 的 VCC3V3，與 ESP32／SHTC3 共用，**無獨立開關**。
因此不能靠韌體做到「只關 SD 電源、ESP timer 繼續睡眠計時」。現有檔案存取完成後
會 close，睡前 SD.end／SPI.end，再釋放主機腳位讓外部 10k pull-up 保持取消選取；
不能用把所有 SD 腳拉 LOW 的方法省電。下一次 deep-sleep wake 重新初始化 SD 後讀檔。
這是停止通訊／卡片閒置，不是 SD 斷電；也不承諾特定卡的 standby current。

驗證範圍：新增 host 測試覆蓋 REG90 全部 256 組狀態、重複喚醒不重寫、初讀失敗、
寫入失敗、讀回失敗與其他 rail bit 意外變動；另有 boot／sleep 接線契約測試。
Hosted CI／編譯與實板 cold boot、KEY／timer wake、共享 I²C、SD 讀寫、電流比較
尚未執行。本分支尚未刷機，不能宣稱每天掉電 20% 已修復。

## 自動能源遙測

- 目前韌體在低頻 Status API 回報可取得的電池電壓、估算百分比、USB 狀態、刷新耗時與
  從開機到狀態上傳前的完整喚醒週期耗時；既有 Profile 也會回報刷新與喚醒耗時。
- Web「能源」頁保存最近 400 天樣本，提供 7／30／90／365 天電量、電壓、刷新耗時、
  完整喚醒時間與最近樣本；所有資料都由裝置自動回報。
- 頁面不再提供電池容量、待機電流、喚醒平均電流或安全保留量表單，也不計算依賴
  人工量測的續航模型。TG28 遙測只協助診斷，不會控制裝置是否可用；續航與睡眠電流
  仍須以實板量測，不能由 register 讀值推導為已通過。

## 編譯

以下是 Hosted CI 所用固定工具鏈的重現參考；一般開發不得在本機編譯，見[AGENTS.md](../../AGENTS.md)。

工具鏈固定為 Arduino CLI 1.5.1、ESP32 core 3.3.10、GxEPD2 1.6.9、ArduinoJson 7.4.3。

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

以上 Flash／RAM 數字只屬於 2026-07-19 歷史 binary，不代表 2.8.6 的剩餘容量。現行 4 MiB／16 MiB repository-owned partitions 與 artifact 大小需以同一 source 的 Hosted CI 核對；OTA 簽章與啟用流程仍未實作。

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
