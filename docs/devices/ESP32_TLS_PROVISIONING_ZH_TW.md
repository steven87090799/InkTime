# ESP32-S3 TLS Trust Anchor 與首次配對

目前 PhotoPainter 韌體的後端傳輸集中在 [`device_http_transport.h`](../../esp32/ink-display-7C-photo/device_http_transport.h)／[`device_http_transport.cpp`](../../esp32/ink-display-7C-photo/device_http_transport.cpp)。所有 Manifest、Queue、Payload、offline schedule、status、ACK 與檔案下載都必須經過同一個 transport；不得在功能函式內直接呼叫 `HTTPClient.begin(url)`。

## Secure build 預設

- 正式建置 `INKTIME_ALLOW_INSECURE_DEVICE_HTTP=0`。
- URL 必須是 HTTPS；公開 HTTP 永遠拒絕，HTTPS 沒有有效 trust anchor 也 fail-closed。
- 禁止 `WiFiClientSecure::setInsecure()`。
- `HTTPClient` 停用 redirect follow；3xx 不會被裝置靜默導向其他 host。
- HTTPS trust anchor 只能來自編譯期 `INKTIME_DEVICE_ROOT_CA` 或已驗證的 Root CA PEM 設定，不會從遠端 response 接受 CA。
- Bearer device token 不會寫入 pairing screen、URL、序列埠或 status 回報以外的診斷文字。

編譯期 provisioning 的最小概念如下；實際建置系統應透過受控 secret／board-specific build property 注入，不要把私有 CA key 或裝置 Token commit 到 Git：

```text
-DINKTIME_DEVICE_ROOT_CA="-----BEGIN CERTIFICATE----- ... -----END CERTIFICATE-----"
```

若使用 Web 配對頁輸入 CA，韌體只接受 64–3500 bytes 且同時包含 `BEGIN CERTIFICATE`／`END CERTIFICATE` 的 PEM；上限由 `device_http_transport.h` 的 `kMaxDeviceCaPemBytes` 與 portal `maxlength` 共用。它會保存到裝置 NVS 的 `ca_pem`；CA 不是 secret，但仍應使用正確的 server CA，不要貼入 server private key。

`saveConfig()` 會檢查每個 NVS string／numeric write 的 return value，並 read-back 比對 `ssid`、`hostport`、`ca_pem`。格式或 CA policy 失敗回 `PAIRING-NVS-001`，NVS namespace 開啟失敗回 `PAIRING-NVS-002`，寫入後 read-back 不一致回 `PAIRING-NVS-003`；任一失敗都不會清除 portal 或重啟裝置。CA 欄位留白仍保留既有 NVS CA。

## 受控 LAN development build

只有明確編譯 `INKTIME_ALLOW_INSECURE_DEVICE_HTTP=1` 才允許 HTTP，而且 host 必須是 localhost、`.local`、`.lan`、`.internal` 或 RFC1918／loopback 位址。這不是正式部署替代方案：

```text
-DINKTIME_ALLOW_INSECURE_DEVICE_HTTP=1
```

LAN build 的設定頁會顯示沒有 TLS 保護的警告。正式 production compose／ESP32 secure build 不應依賴這個旗標；CI 的 LAN smoke 只驗證隔離測試拓撲，不能作為真實網路安全證據。

## 首次配對流程

當裝置沒有有效 Wi-Fi 設定時，`startConfigPortal()`：

1. 產生隨機 AP SSID（`InkTime-<裝置短 ID>`）與隨機 24 字元十六進位 AP password；密碼不由 MAC、chip ID 或 SSID 推導。
2. 啟動 AP 與 `http://192.168.4.1/` 設定頁；配對授權 secret、nonce、嘗試次數與五分鐘 expiry 仍由韌體限制。
3. 若是 PhotoPainter，先在電子紙上顯示 `INKTIME PAIRING`、Wi-Fi SSID、AP password、setup URL 與 `VALID 5 MIN`，因此不必依賴序列埠才能完成配網。
4. Web 表單可保存 Wi-Fi、HTTPS backend URL、Root CA PEM、device Token、時區、刷新時間與 180° 設定；保存前會再次驗證 URL／CA，且 NVS write/read-back 必須成功。
5. 成功保存後停止 portal、清除 pairing secret／nonce 並重啟；超時或失敗嘗試達上限時進入 bounded sleep。

AP password 是短期配對資訊，不是後端 Bearer Token。使用者完成設定後應讓 AP portal 結束，並在管理頁確認該裝置的 Token 與 delivery mode。

## 診斷錯誤碼

| 錯誤碼 | 含義 | 處理 |
|---|---|---|
| `DEVICE-URL-INVALID` | scheme、host、userinfo 或 fragment 不符合 | 填完整 `https://host[:port]` URL，不要帶帳密。 |
| `DEVICE-TLS-NO-TRUST` | HTTPS 沒有有效 CA | 重新注入 compile-time CA 或在 portal 貼正確 Root CA PEM。 |
| `DEVICE-TLS-BEGIN` | secure client 初始化失敗 | 先確認 CA、heap 與 server certificate chain；不要改成 `setInsecure()`。 |
| `DEVICE-HTTP-DISALLOWED` | secure build 收到 HTTP | 改用 HTTPS；只有隔離開發 build 才可開 LAN HTTP flag。 |
| `DEVICE-HTTP-PUBLIC-DISALLOWED` | LAN build 收到公開 HTTP host | 改用 HTTPS 或受控私有 host。 |
| `PAIRING-NVS-001` | CA／設定 policy 不合法，或 NVS string／numeric write 失敗 | 修正 CA／欄位內容後重試；不要重啟或改用 `setInsecure()`。 |
| `PAIRING-NVS-002` | NVS namespace 無法開啟 | 檢查裝置儲存狀態與硬體；設定未視為成功。 |
| `PAIRING-NVS-003` | NVS write 後 read-back 不一致 | 保留現有設定，檢查 NVS 容量與 CA 長度後再重試。 |

## 實機驗收邊界

Hosted CI 可以 compile 多個 ESP32 profile、檢查 secure／LAN contract、redirect policy 與 pairing source contract；它不能證明真實 CA chain、板上 Wi-Fi、PMIC、BUSY waveform、面板 ghosting、深睡電流或 GPIO 行為。真實 PhotoPainter hardware、正式憑證與正式 backend smoke 在本次交付維持 `NOT RUN`，須依 [PhotoPainter 指南](WAVESHARE_PHOTOPAINTER_ZH_TW.md) 的順序另行量測。
