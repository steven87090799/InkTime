# 13.3 吋 E6 六色韌體元件

此目錄保留 13.3 吋、1600×1200 E6 六色面板的 ESP32-D0WD-V3 韌體元件。它的渲染與顯示流程不同於目前主要的 7.3 吋 PhotoPainter／ESP32-S3 路徑。

## 現行邊界

- 根目錄舊腳本 `render_daily_photo_133c.py` 只屬於此 legacy 133C 流程，不是 InkTime Web／Worker／Scheduler 的正式 Release Coordinator。
- 此韌體仍為 beta，尚無本輪長期功耗、深度睡眠、BUSY timing 或實體面板驗收證據。
- 現行正式架構、面板 Profile 與裝置協定請讀 [`../../docs/reference/CURRENT_STATE_ZH_TW.md`](../../docs/reference/CURRENT_STATE_ZH_TW.md) 與 [`../../docs/devices/ESP32_GUIDE_ZH_TW.md`](../../docs/devices/ESP32_GUIDE_ZH_TW.md)。
