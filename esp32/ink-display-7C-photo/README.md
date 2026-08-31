# 7.3 吋／PhotoPainter 韌體入口

目前共用 sketch 韌體版本為 `2.8.6`。現有 GDEY／GDEP PCB 與 Waveshare PhotoPainter 由 compile-time profile 區分，不能只改 Web Profile 就換板。

| 需求 | 文件 |
|---|---|
| 選板、固定依賴、建置與配網 | [ESP32 指南](../../docs/devices/ESP32_GUIDE_ZH_TW.md) |
| Rev2.0／TG28、腳位、安全電源邊界 | [PhotoPainter 基準](../../docs/devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md)、[實板交接](../../docs/devices/PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md) |
| 配對碼、claim／confirm、撤銷與 repair | [自動配對](../../docs/devices/ESP32_AUTOMATIC_PAIRING_ZH_TW.md) |
| HTTPS Root CA 與可信任 LAN | [TLS／配網](../../docs/devices/ESP32_TLS_PROVISIONING_ZH_TW.md) |
| Stock／Online／Enhanced 排程 | [交付模式](../../docs/devices/PHOTOPAINTER_DELIVERY_MODES_ZH_TW.md) |
| 第三方程式授權 | [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) |

Config Store payload v5 可讀舊 v1–v4；24-slot 能力必須經配對確認。2.8.6 的 KEY1 雙擊電源頁在停留 30 秒後從 SD 驗證並恢復最後成功照片；完整按鍵時間與 fallback 請依 PhotoPainter 指南。

一般開發的編譯由 Hosted CI 執行。操作實體板前確認序列裝置身分、備份完整 flash 並保留可恢復副本；app-only 映像寫入 `0x10000`。不要擦除 NVS 或把 app-only 寫到 `0x0`。GPIO0 BOOT、GPIO5 PWR、GPIO21 IRQ 與 TG28 ALDO4 narrow write allowlist 不可任意改動。CI、序列紀錄、真實畫面與功耗是不同驗收證據。
