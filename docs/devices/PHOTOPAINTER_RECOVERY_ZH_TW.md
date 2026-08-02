# PhotoPainter 交付恢復與安全停機

本文件只描述可恢復的軟體與資料邊界。它不授權自動刷機、不授權合併 PR，也不把沒有實機觀測的結果標成完成。

## 上線前停機條件

出現以下任一情況時，先停止該裝置的 Stock upload 或 Enhanced day preparation：

- Release Manifest 找不到唯一 `.bin`、大小不符或 SHA-256 不符。
- Stock Host 不是裸 IP／主機名，或 DNS 解析出 public、reserved、loopback 或 mixed 位址。
- Stock 回應未知、timeout、redirect 或非 2xx。不要直接重送同一個 `/dataUP`。
- Enhanced schedule 的 Slot 數量、順序、時區、config version 或 Queue Item identity 不一致。
- Device 回報 `DISPLAY_FAILED`、BUSY timeout、Flash/PSRAM/SD/RTC failure，或顯示結果尚未由實機確認。

## Server 端恢復

1. 在 Web Review Workbench 先確認照片與 Release，不直接從原始路徑手工拷貝檔案。
2. 檢查裝置的 `delivery_mode`、`stock_endpoint_host`、`schedule_times`、`prefetch_lead_minutes` 與 config version。
3. Enhanced 模式只重新建立尚未顯示的 target date；讓單一 transaction 重新產生一組 Slot／Release／Queue Item，不手動插入半套資料。
4. 若 queue version 已改變，讓裝置重新取得 Manifest；不要用舊 version 強行 ACK。只有符合 offline delayed-terminal 規則的 terminal event 才可使用例外。
5. Stock upload 若曾進入「回應未知」，保留人工確認狀態；server 不以 timeout 推定成功，也不做盲目 retry。

## 裝置端恢復

- `legacy_online` 與 `stock_compat` 維持既有 firmware bring-up；Stock 相容不要求刷入 Enhanced firmware。
- Enhanced local-only wake 只讀已驗證的本地正式 Frame；沒有完整 SHA-256 或 cache/header/CRC 不符時保留舊畫面並進入下一個 exact epoch sleep，不回退成無限制網路重試。
- 長按 GPIO4 才可請求 network refresh；短按是本地下一個動作。GPIO0 保留 BOOT，GPIO5 保留原廠 PWR 用途，不能拿來改作一般 output。
- Cache 使用 bounded header、full SHA-256（新格式）與 CRC；`.tmp` → `.bak` → formal file 的原子替換失敗時，刪除不完整檔案並保留可恢復版本。

## 觀測與驗證邊界

伺服器事件可證明的是授權、轉換、HTTP status、Queue 狀態與資料庫原子性；它不能證明 Stock 面板已刷新。實機恢復必須另外記錄 board revision、firmware commit、供電、BUSY waveform、刷新時間、PMIC/SD/RTC 狀態、畫面方向與待機電流。這些量測尚未在本輪執行，狀態是 `NOT RUN`。
