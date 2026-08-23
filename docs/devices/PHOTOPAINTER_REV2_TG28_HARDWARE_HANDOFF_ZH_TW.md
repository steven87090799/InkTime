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
