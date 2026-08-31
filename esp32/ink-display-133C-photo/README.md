# 13.3 吋 E6 韌體（歷史 beta，未接入現行發布流程）

這個目錄保留 ESP32-D0WD-V3／13.3 吋六色面板的實驗程式。面板為 1600×1200；程式的直向 framebuffer 為 1200×1600、4bpp、960,000 bytes，分成左右兩個 480,000-byte 檔案。

目前 sketch 仍使用 `/static/inktime/<key>/photo_13in3_6c_*_L.bin`／`_R.bin` 舊下載方式，尚未接入版本化 Device Secret、Manifest／Queue ACK。主線已退休該 Legacy server 路由；現行 Web 只有 480×800 的三種 7.3 吋 Profile，**不提供可直接選用的 13.3 吋正式 Release**。

不要把 7.3 吋 BIN、PhotoPainter 腳位或分割區套到此板，也不要為了使用本 sketch 重新開放 URL 金鑰。若要正式支援，需另行實作並驗證 server Profile、renderer、雙檔協定、安全認證及實體面板；長期穩定性與功耗仍未驗收。

現行支援範圍見[版本基線](../../docs/reference/CURRENT_STATE_ZH_TW.md)與[7C／PhotoPainter 元件](../ink-display-7C-photo/README.md)。
