# PhotoPainter Rev2.0 TG28 實板除錯交接紀錄

> 狀態日期：2026-08-23
>
> 適用硬體：Waveshare ESP32-S3-PhotoPainter Rev2.0、ESP32-S3-WROOM-1 N16R8、TG28 PMIC
>
> 目的：讓後續維護者或 AI 直接從已完成的官方／InkTime 實板 A/B 繼續，不重做危險實驗。

本文件是硬體事故與驗收交接，不取代一般使用說明。現行腳位、編譯、配對與操作契約仍以
[PhotoPainter 支援與安全基準](WAVESHARE_PHOTOPAINTER_ZH_TW.md)為準。GitHub／CI／PR
狀態會變動，使用前必須重新查詢；原理圖接線、已觀察的實板現象與安全禁區不得因舊的
AXP2101 假設而被覆寫。

## 上游依據

- [Waveshare PhotoPainter Wiki](https://www.waveshare.com/wiki/ESP32-S3-PhotoPainter)：Rev2.0
  schematic、TG28 datasheet、燒錄與原廠操作資源入口。
- [Waveshare 官方 repository](https://github.com/waveshareteam/ESP32-S3-PhotoPainter)。
- 本輪交叉核對的固定上游版本：
  [`a5e8f757ba0cafbb5586f07d3e83bda3184c0845`](https://github.com/waveshareteam/ESP32-S3-PhotoPainter/commit/a5e8f757ba0cafbb5586f07d3e83bda3184c0845)。

原理圖與 TG28 datasheet 是判定 ALDO4 的來源；官方程式是傳輸流程與實板 A/B 基準。
不要只複製官方程式中「同時開啟多個 LDO」的結果，應以原理圖的實際 net 與 TG28
register 定義縮小為 InkTime 所需的單一 EPD rail。

## 一句話根因

先前 InkTime 把 TG28 **ALDO3** 誤認為 `EPD_VCC`；Rev2.0 原理圖的實際電子紙電源是
**ALDO4**。官方 factory 同時開啟 ALDO3 與 ALDO4，因而掩蓋錯誤：先跑官方韌體後的
InkTime 暖啟動可能刷新，但完整斷電後只開 ALDO3，電子紙 controller 沒有真正供電，
最後出現 `EPD-BUSY-NOT-ASSERTED` 與畫面不變。

## 已確認的硬體事實

| 項目 | Rev2.0 實板契約 |
|---|---|
| PMIC | TG28，I²C `0x34`；不要用 AXP2101 chip-ID／register 假設 |
| `EPD_VCC` | TG28 ALDO4；電壓 `REG95[4:0]=0x1C`（3.3 V），enable `REG90[3]=1` |
| EPD | DC 8、CS 9、SCK 10、MOSI 11、RST 12、BUSY 13；BUSY active-low |
| I²C | SDA 47、SCL 48、100 kHz |
| SD | CS 38、SCK 39、MISO 40、MOSI 41；與 EPD 分開的 SPI bus |
| 按鍵 | GPIO0 BOOT、GPIO4 KEY1、GPIO5 PWR；三者不可互換用途 |
| 指示燈 | GPIO45 PWR 紅燈、GPIO42 ACT 綠燈，皆 active-low；不是 GPIO5 PWR 按鍵 |
| Flash／PSRAM | 16 MiB Flash、8 MiB OPI PSRAM；app offset `0x10000` |

TG28 的 narrow write allowlist 只有 `REG95` 的 ALDO4 voltage bits 與 `REG90[3]`。程式
以 read-modify-write 保存其他 bit，並在每次寫入後 readback。不要寫 DCDC、充電、
全機 shutdown、fast-power-on 或其他 LDO register。

## 2026-08-23 官方完整 GPIO／電源對齊稽核

本節固定以官方 repository `a5e8f757…`、Rev2.0 schematic 與 TG28 datasheet 三方交叉
核對。官方 README 1.4.0 起已明確將 PWR 改為只負責開／關機；Wiki 仍保留「短按 PWR
進入休眠」的舊文字。操作語意衝突時，以目前官方 README／source 與實際電路為準，
不得把 Wiki 舊文字重新實作成 GPIO5 軟體 sleep。

| GPIO／電源軌 | Rev2.0 電路與官方用途 | InkTime 現況 | 判定／注意事項 |
|---|---|---|---|
| GPIO0 | BOOT，active-low；官方單擊／雙擊／長按另有應用功能 | 完全保留，不取樣、不驅動 | 正確；避免影響下載模式。InkTime 改由 GPIO4 喚醒是刻意的產品差異 |
| GPIO1／2 | SDMMC D1／D2，10 kΩ pull-up | 不配置；SD 採 1-bit SPI | 正確且較保守；吞吐量較低，不是接錯腳 |
| GPIO3 | TG28 `CHGLED` 訊號 | 不配置 | 正確；不得當 LED output 使用 |
| GPIO4 | KEY1，active-low、外部 10 kΩ pull-up | runtime input、EXT1 `ANY_LOW` wake | 正確；已完成短按喚醒刷新實測 |
| GPIO5 | `SYS_OUT`，由 PWRON／MOSFET 電路形成 active-high PWR 按鍵狀態 | 完全不驅動，也不作 InkTime wake | 正確；實體 PWR 仍由 TG28 處理，不是一般 GPIO output |
| GPIO6 | PCF85063 `RTC_INT`，並經二極體接到 PWRON 網路 | 不配置 | 尚未使用 RTC alarm；不是目前 ESP timer wake 的必要腳位 |
| GPIO7 | NS4150B `AudioCTR`／PA enable | 固定 LOW | 正確；只關閉功放，不等於 ES7210／ES8311 的 `Audio_VCC` 已斷電 |
| GPIO8～13 | EPD DC／CS／SCK／DIN／RST／BUSY | 完全同官方接線；BUSY active-low | 正確；BUSY cycle 與畫面變化才是刷新證據 |
| GPIO14～18 | I²S MCLK／SCLK／LRCK／DSDIN／DSOUT | 不初始化音訊 | 接線定義正確；閒置音訊晶片是否耗電仍取決於 ALDO2／codec 狀態 |
| GPIO19／20 | ESP32-S3 原生 USB D-／D+ | USB CDC／JTAG | 正確；不得另作一般 GPIO |
| GPIO21 | TG28 IRQ，open-drain，4.7 kΩ pull-up | 完全不驅動 | 正確；官方功耗測試範例曾把它設 output 拉低／高，InkTime 不複製此危險動作 |
| GPIO38～41 | SD CS／CLK／MISO／MOSI | 獨立 SPI bus，20 MHz→4 MHz fallback | 正確；與 EPD SPI3 無重疊 |
| GPIO42 | ACT 綠燈 cathode，4.7 kΩ、active-low | 僅在 EPD transaction／refresh 期間亮 | 正確；不代表網路或 SD activity |
| GPIO43／44 | UART0 RX／TX | 正式 PhotoPainter 走 native USB CDC；未改作板級輸出 | 正確；可保留 UART recovery 能力 |
| GPIO45 | PWR 紅燈 cathode，4.7 kΩ、active-low；也是 ESP32-S3 strap 腳 | global constructor 階段拉 LOW，deep sleep 前拉 HIGH | 接線／極性正確；strap 在 reset 時已取樣，constructor 才驅動不會改變本次 strap；亮燈不等於 boot 已完成 |
| GPIO46 與未列出的 module 腳 | schematic 未配置為 PhotoPainter 功能，部分腳亦受 N16R8 memory／strap 限制 | 不使用 | 正確；不得因「看似空閒」自行分配 |
| I²C GPIO47／48 | Audio、TG28、PCF85063、SHTC3 共用 SDA／SCL | 100 kHz、open-drain recovery、bounded retry | 正確；官方 app 也是 100 kHz，factory 測試的 300 kHz 不應蓋過共享裝置保守值 |
| TG28 ALDO2 | schematic 僅接 `Audio_VCC` | 不寫 REG90[1]／REG93 | 安全但功耗未定；PA LOW 不能證明 codec rail 關閉 |
| TG28 ALDO4 | schematic 直接接 `EPD_VCC` | 3.3 V、refresh 前確保 enabled；deep sleep 保持 enabled | 刷新正確；待機功耗仍須量測，不能直接關閉 |
| TG28 DCDC1 | `VCC3V3`，供 ESP32、SD、SHTC3 等板級負載 | 不改 PMIC 設定 | 正確；ESP timer／GPIO4 wake 需要主系統仍可運作 |

兩顆 LED 的 4.7 kΩ 串聯電阻已核對正確；單顆 LED 的絕對理論上限小於
`3.3 V / 4.7 kΩ = 0.71 mA`，實際值還要扣除 LED forward voltage。PWR 紅燈只在醒著時
增加小量消耗，兩燈在 deep sleep 前都設為 OFF；因此燈號不是最可能的待機耗電主因。
但紅燈熄滅只代表 InkTime 已將 GPIO45 設成 OFF，不足以證明 TG28 已進入全機 power-off。

### 與官方不同但目前屬於合理設計的項目

- 官方最新功能程式的 EPD SPI 是 40 MHz；InkTime 固定 10 MHz，因為同一實板已驗證
  通過的官方 factory／功耗測試 driver 是 10 MHz。192,000-byte frame 的傳輸多約
  0.1 秒等級，遠小於約 25～30 秒的實際電子紙刷新，不值得用穩定性換取。
- 官方以 4-bit SDMMC 使用 GPIO1／2／38～41；InkTime 用 GPIO38～41 的 SPI 模式。
  這會降低 SD 吞吐，但減少腳位與 driver 複雜度，不影響電子紙接線。
- 官方用 BOOT GPIO0 操作圖片／電池資訊／sleep；InkTime 保留 GPIO0 給下載模式，改用
  GPIO4 KEY1 喚醒、forced refresh 與 recovery。這是明確的人機介面差異，不是 GPIO 錯配。
- 官方 1.3.0 起每次 boot 寫 500 mA 充電電流與 2 A VBUS limit；InkTime 故意不寫充電、
  VBUS 或 TS／JEITA register。這對安全較保守，但代表暖啟動後的實際充電設定可能承接
  先前韌體狀態，完整 POR 後則依 TG28 eFuse／register reset；不能只由 InkTime source
  宣稱固定為 500 mA。

### 已找出的剩餘問題與安全修復順序

1. **睡眠電流仍是 NOT RUN。** 官方產品頁宣稱 sleep current `≤ 1 mA`，但官方功能程式
   自己也把 `axp_basic_sleep_start()` 註解掉；只有功耗測試範例會設定 REG26 PMIC sleep
   並關閉多路 LDO。InkTime 與官方功能程式一樣採 ESP deep sleep、EPD `POWER_OFF`，
   不能用官方規格反推 InkTime 已達標。
2. **Audio_VCC 可能是未量測的常駐負載。** schematic 證明 ALDO2 只供 ES7210／ES8311，
   但 InkTime 目前只將 GPIO7 PA 拉 LOW。先增加 REG90／REG93 的唯讀診斷與 A/B 電流
   證據；未量測前不要擴大 PMIC write allowlist。若證明差異顯著，再以獨立測試分支只做
   REG90[1] read-modify-write，並驗證 cold boot、I²C、音訊保留與 full recovery。
3. **ALDO4／EPD_VCC 待機成本未知。** 不可直接套用 factory 的全 LDO shutdown；先前廣泛
   rail 變更曾讓共享 I²C 在 ESP-only reset 後卡 low。優先量測；若 ALDO4 是主要差異，
   才設計「sleep 前 snapshot REG80／90／91、受控 PMIC sleep、wake 後 readback／restore」
   的可恢復實驗，而且每一步都保留 timer／KEY1 wake 與官方備份恢復路徑。
4. **SD 沒有獨立 power gate。** `SD.end()` 只關閉 host／bus，卡片仍接 VCC3V3；不同卡的
   standby current 可能差很多。以「無卡／同一張卡」做睡眠電流 A/B；若差異超標，優先
   改用低待機卡或允許無卡運行，而不是關閉 DCDC1 讓 ESP timer wake 一起失效。
5. **充電行為不具 source-level 固定值。** 先唯讀回報 REG16、REG18、REG50、REG58、
   REG62～65 與 TS／thermal status；確認電池容量、NTC 與溫升前，不要照抄官方 500 mA。
   若要固定充電參數，必須做 USB 供電、邊充邊 refresh、滿充與異常溫度實測。
6. **電量百分比不是校準證據。** TG28 datasheet 要求用實際 battery model 資料改善 fuel
   gauge；官方 repository 沒有提供這顆隨附電池的完整 model。InkTime 正確地不以 A4
   百分比決定是否刷新，但 UI／log 應把它視為估算，低電壓策略應同時看實測 VBAT、負載
   壓降與 PMIC 保護，不能猜一個門檻直接寫死。
7. **10 分鐘 max-awake 已有 persistent-fault backoff。** 前兩次 timeout 仍以 restart
   嘗試恢復；第三次後改進入 60 分鐘 deep sleep，並保留 GPIO4 recovery 與 timer wake。
   計數只放 RTC no-init memory，不在每輪寫 NVS；正常 sleep、完整斷電或明確 recovery
   會清除計數。這項修補避免永久故障形成無限高耗電 boot loop，但尚未以實板 fault
   injection 驗收，不能把 Hosted compile 當成電池續航或 safe-sleep 實證。
8. **長時間 timer wake 尚未實測。** 現行排程使用 ESP timer wake，PCF85063 GPIO6 alarm
   尚未啟用。先做 8 小時 timer wake 的實板時間誤差與刷新驗收；只有誤差不合格時，才
   評估 GPIO6 RTC alarm。GPIO6 又透過二極體連到 PWRON，因此不可只照一般 RTC 開發板
   範例接入，必須連同 TG28 power-on 行為測試。
9. **LED 腳位尚未完全納入中央 BoardConfig。** EPD、SD、I²C、按鍵與音訊都有
   compile-time pin assertions，但 GPIO45／42 目前是 `photopainter_support.cpp` 的固定
   常數，沒有同等的中央 static assertion。現值與官方完全一致；後續修復應把 indicator
   pins／active-low 語意納入 board profile 或至少新增 compile-time contract，避免未來
   重構時燈號悄悄漂移。
10. **PWR 燈的時間點比官方更早、錯誤語意較少。** InkTime 在全域 constructor 就點亮
    GPIO45，因此只能表示 MCU 已進入韌體早期階段，不代表 PMIC／PSRAM／I²C／SD／EPD
    全部 ready；官方則會依網路狀態閃燈。後續應建立明確 state machine（active、pairing、
    hardware/display error、sleep），ACT 仍只綁真實 EPD transaction。若保留早期常亮，
    文件與 UI 必須持續說明 PWR 燈不是「整機驗收通過」訊號。

功耗驗收必須至少分開記錄：PWR 長按後的 TG28 全機 off baseline、官方功能韌體 deep
sleep、InkTime deep sleep、拔 SD 卡 A/B、以及 refresh peak／30 秒平均。USB 供電同時
包含系統負載與充電電流，不能拿一般 USB 表的總電流直接當 battery-only sleep current。
沒有正確串接與保險的 power profiler／battery emulator 時，寧可維持 `NOT RUN`，不要為
了量數字冒反接電池或短路風險。

## 能穩定刷新的傳輸條件

下列不是任意最佳化，而是同一 Rev2.0 實板與官方 factory 對照後保留的工作路徑：

- 在全域 `PhotoPainterSupport` constructor 階段建立並持續保留 ESP-IDF SPI3 bus；不要在
  `setup()` 後再以 Arduino `pinMode` 重設 SCK／MOSI，否則可能改寫已建立的 pin matrix。
- SPI3 half-duplex、MODE0、手動 CS、10 MHz、5000-byte polling transaction。
- framebuffer 的 CS low 期間不 `yield()`；控制腳使用 ESP-IDF GPIO API。
- `0xAA` 後六個參數逐 byte 各自完成 CS low／high framing，與已通過的官方 factory 相同。
- ALDO4 原已開啟時等待 10 ms；真正由關閉轉為開啟時，RESET high 下等待 500 ms。
- `EPD_Init` 後等待 3 秒才傳第一張 framebuffer。
- 真正 `DISPLAY_REFRESH` 必須觀察到 BUSY assertion 再回 ready；只有 BUSY 上拉為 high
  不能當作刷新成功。

對應主要實作位於：

- `esp32/ink-display-7C-photo/photopainter_support.cpp`
- `esp32/ink-display-7C-photo/spectra6_73.cpp`
- `esp32/ink-display-7C-photo/hardware_profile.h`

## 不得重做的危險或誤導路徑

- 不得再把 `EPD_VCC` 改回 ALDO3（`REG94`／`REG90[2]`）。
- 不得把 Rev2.0 當 AXP2101，亦不得用 AXP2101 專用 identity 契約決定是否供電。
- 不得驅動 GPIO21；它是 TG28 IRQ/open-drain 輸出，不是 ESP32 可拉高低的控制腳。
- 不得把 GPIO5 當一般 output；它保留原廠 PWR 用途。GPIO0 只保留 BOOT／下載模式。
- 不得在一般 refresh 或 deep sleep 前關閉 ALDO4。實板曾在 rail 被清除後的 ESP-only
  reset 觀察到共享 I²C 持續為 low；現行程式只讓面板 controller `POWER_OFF`。
- SDA／SCL 卡 low 時不得 push-pull 強拉高，也不要連續 reset／重刷碰運氣。先停止 PMIC
  與 EPD 命令，採完整板級斷電恢復。
- 不得把「官方 HTTP 接受圖片」、「序列 log」、「Hosted CI」或 simulator 當作真實
  電子紙刷新；必須由面板實際變化確認。
- 完整 16 MiB flash 備份可能含 Wi-Fi、Token 或其他秘密，不得上傳 GitHub、貼到 PR、
  放入 artifact 或顯示其內容。備份只作本機可恢復用途。

## 完整斷電恢復程序

僅在 I²C 卡 low、板級狀態不明或明確需要 factory recovery 時使用：

1. 停止重複 reset、刷機與 PMIC 寫入。
2. 拔除 USB。
3. 依原廠電源流程長按 PWR 約 5 至 6 秒，直到 PWR 燈熄滅。
4. 等待至少 10 秒，讓 TG28 與共享 I²C 狀態真正清除。
5. 接回 USB，再依原廠流程開機。

單純拔插 USB 或 esptool 的 ESP-only reset 不一定能清除 PMIC 狀態。若板子異常發熱、
有異味、電池膨脹或 USB 反覆斷線，立即停機，不繼續嘗試。

## 2026-08-22 至 2026-08-23 實板證據

| 驗證 | 結果 | 證據邊界 |
|---|---|---|
| 燒錄前 16 MiB 完整備份 | PASS | 可恢復；原始備份未進 GitHub，亦不得公開 |
| 官方完整備份恢復 | PASS | BOOT 操作後官方電池資訊畫面有變化 |
| 官方 factory 白／黑刷新 | PASS | 同一板、同一面板可刷新，排除硬體故障 |
| 原 InkTime 完整冷啟動 | FAIL | `EPD-BUSY-NOT-ASSERTED`，畫面不變 |
| 官方先初始化後的 InkTime 暖啟動 | 有時 PASS | ALDO4 殘留狀態會掩蓋錯誤，不可當冷啟動證據 |
| ALDO4 InkTime 暖啟動 | PASS | `pairing_display_ready`，配對畫面約 30 秒刷新 |
| ALDO4 真正電池冷啟動 | PASS | 拔 USB、完整關機等待、電池重新上電後面板有變化 |
| 最終指示燈版暖啟動 | PASS | `pairing_display_ready` 30.101 秒；面板與 ACT 均有變化 |
| deep sleep → KEY1 | PASS | 拔 USB 進入睡眠後短按 GPIO4，喚醒並再次刷新 |

真正冷啟動期間 USB CDC 必然中斷，早期 boot log 可能在 USB 尚未重新枚舉時遺失。
「沒有捕捉到冷啟動序列 log」不能推導為沒有開機；這一項以實體面板變化驗收。

## 已保存的版本身分

- ALDO4 根因修正 commit：`30507c7`。
- PWR／ACT 與 KEY1 實板驗收 commit：`20e014a`。
- 交付分支：`fix/photopainter-flash-serial-hardware-validation`。
- Draft PR：`#94`。PR 是否仍開啟、Draft、exact-head CI 狀態必須即時重查。
- 最終實機候選 Debug app SHA-256：
  `187cd069b554f3554248ab58c77f9e8ae4ed4028e797905c113a5652b467573a`。
- 對應 repository-owned partition bin SHA-256：
  `a1b1b88aa50e14f19c64ab976bf7382aec0cdee563451cf0cb127837dc01b16b`。

候選韌體以 app-only `0x10000` 更新，沒有覆寫 bootloader、NVS 或整顆 flash。上述 hash
只用來辨識已驗收 binary，不代表 binary 應提交 repository；正式交付仍由 exact-head
Hosted CI 重新編譯。

## 後續 AI 的起手順序

1. 先讀本文件、`WAVESHARE_PHOTOPAINTER_ZH_TW.md`、worktree 的 `AGENTS.md`。
2. 只讀檢查目前 worktree、branch、dirty files、PR head、`origin/main` 與 Hosted CI；
   不假設本文件記載的 PR 狀態仍然有效。
3. 確認板型確實是 Rev2.0/TG28，再核對 ALDO4 constants、SPI3 persistent transport 與
   GPIO0／5／21 安全 fence 仍存在。
4. 若只是繼續功能驗收，不要先恢復官方韌體；官方 A/B 已完成。只有 recovery 或新硬體
   差異才重新跑 factory，且不得上傳完整 flash。
5. 變更韌體後先做允許的靜態檢查與 exact profile build，再以明確裝置身分 app-only
   燒錄；保留可恢復備份與 repository-owned partition table。
6. 每個結果分開記錄：靜態檢查、Hosted CI、序列 log、實體燈號、實體畫面。不得用前
   三者替代最後兩者。
7. 不輸出配對 SSID 密碼、Wi-Fi 密碼、Device Secret、Token 或完整 flash 內容。

## 尚未完成的實板驗收

以下截至本紀錄日期仍是 `NOT RUN`，不得宣稱整機全部功能完成：

- 完整後端自動配對與 Device Secret claim／confirm。
- 真實正式六色照片下載、色序、方向與殘影。
- 真實 SD、cache、正式 frame 與斷電恢復。
- timer 排程喚醒；GPIO4 KEY1 喚醒已通過，但不能代替 timer。
- 睡眠電流、刷新峰值電流與長期電池續航。

下一個安全驗收階段應從完整後端配對開始，再測正式六色照片與 timer 排程喚醒；除非
出現新的硬體證據，不應回頭重做 ALDO3／AXP2101 或廣泛 PMIC 實驗。
