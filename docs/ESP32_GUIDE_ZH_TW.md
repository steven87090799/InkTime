# ESP32 指南

## 編譯

Arduino IDE／CLI 安裝 ESP32 Core、`GxEPD2`、`ArduinoJson`。開啟 `esp32/ink-display-7C-photo`，選 ESP32-S3 並啟用 PSRAM。

## 配對

在 InkTime「裝置」新增裝置，複製只顯示一次的 `itd_...` Token。ESP32 AP 設定頁填入 Wi-Fi、伺服器與裝置 Token；Token 不會回填或印到序列埠。

## 傳輸

裝置以 `Authorization: Bearer` 取得 `/api/device/v1/releases/latest`，確認 schema、480×800、2bpp，再隨機選檔。下載必須是 96,000 bytes 且 SHA-256 符合，成功才解包；失敗會嘗試其他檔案，全部失敗不刷新墨水屏。

舊 URL 金鑰模式預設關閉且不建議公網使用。遺失 Token 時從 UI 重新產生；舊 Token 立即撤銷。
