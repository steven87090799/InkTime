# ESP32-S3 TLS Trust Anchor 與首次配對

目前 PhotoPainter 韌體的後端傳輸集中在 [`device_http_transport.h`](../../esp32/ink-display-7C-photo/device_http_transport.h)／[`device_http_transport.cpp`](../../esp32/ink-display-7C-photo/device_http_transport.cpp)。所有 Manifest、Queue、Payload、offline schedule、status、ACK 與檔案下載都必須經過同一個 transport；不得在功能函式內直接呼叫 `HTTPClient.begin(url)`。

## Transport 預設

- Waveshare PhotoPainter 預設可直連 literal RFC1918 IPv4 HTTP（`10/8`、`172.16/12`、
  `192.168/16`），讓家用 Mac／NAS 不需要先配置 TLS Root CA；其他 ESP32 profile 仍以
  `INKTIME_ALLOW_INSECURE_DEVICE_HTTP=0` 維持 HTTPS-only 預設。
- 公開 IPv4、hostname、loopback、link-local 與 IPv6 HTTP 永遠拒絕；HTTPS 仍可使用
  hostname，但沒有有效 trust anchor 時會 fail-closed。
- 禁止 `WiFiClientSecure::setInsecure()`。
- `HTTPClient` 停用 redirect follow；3xx 不會被裝置靜默導向其他 host。
- HTTPS trust anchor 只能來自編譯期 `INKTIME_DEVICE_ROOT_CA` 或已驗證的 Root CA PEM 設定，不會從遠端 response 接受 CA。
- Device Secret／Legacy Bearer credential 不會寫入 pairing screen、URL、序列埠或 status 回報以外的診斷文字；短效 pairing code 只在裝置畫面出現，管理員核准表只提供輸入框，不回顯伺服器配對碼。

編譯期 provisioning 的最小概念如下；實際建置系統應透過受控 secret／board-specific build property 注入，不要把私有 CA key 或任何裝置 credential commit 到 Git：

```text
-DINKTIME_DEVICE_ROOT_CA="-----BEGIN CERTIFICATE----- ... -----END CERTIFICATE-----"
```

若使用 Web 配對頁輸入 CA，韌體只接受 64–3500 bytes、包含 `BEGIN CERTIFICATE`／`END CERTIFICATE` 且能通過 mbedTLS X.509 parse 的 PEM；私鑰、截斷與垃圾內容都拒絕。上限由 `device_http_transport.h` 的 `kMaxDeviceCaPemBytes` 與 portal `maxlength` 共用。正式設定會以完整 payload 寫入 `cfgstore` 的 A/B blob，`dashcfg` 的舊形式 key 僅作一次性 migration input；CA 不是 secret，但仍應使用正確的 server CA，不要貼入 server private key。

`saveConfig()` 會先驗證 CA policy，再以 generation、CRC、active pointer 與 read-only full-payload read-back 完成 A/B commit。格式或 CA policy 失敗回 `PAIRING-NVS-001`，NVS namespace 開啟失敗回 `PAIRING-NVS-002`，寫入後 full-payload read-back 不一致回 `PAIRING-NVS-003`；pointer／journal decode failure 使用 `PAIRING-NVS-004`／`PAIRING-NVS-005`，移除或 clear 後 read-only 檢查失敗回 `PAIRING-NVS-006`，pointer restore 本身失敗回 `PAIRING-NVS-007`。legacy cleanup 失敗時保留新的 canonical journal／A/B blob，等待下次 retry。任一正式 commit 失敗都不會切換舊 active pointer、清除 portal 或重啟裝置。空字串是正式值，會覆蓋舊 password、CA 或 backend hostport；Device Secret／Legacy credential 不由新配網表單重新輸入，Factory Reset 才會清除。

## 受控 LAN HTTP

PhotoPainter 的一般建置已啟用嚴格 RFC1918 literal IPv4 HTTP；其他 profile 只有明確編譯
`INKTIME_ALLOW_INSECURE_DEVICE_HTTP=1` 才啟用相同政策。這不是公開 Internet 部署替代方案：

```text
-DINKTIME_ALLOW_INSECURE_DEVICE_HTTP=1
```

HTTP hostname 沒有相容開關，也不會因 DNS 結果落在私網而放行；請直接輸入 Mac／NAS 的
RFC1918 IP。Production HTTPS、CA 驗證與 secure cookie 仍是跨網段／公開部署的必要路徑。
CI 的 LAN contract 只驗證程式與隔離測試拓撲，不能作為真實網路安全證據。

## 首次配對流程

當裝置沒有有效 Wi-Fi 設定時，`startConfigPortal()`：

1. 產生 AP SSID（`InkTime-<裝置短 ID>`）與每個 AP session 重新產生的 8 位數字密碼；
   密碼使用 ESP32 hardware random source，不由 MAC、chip ID、SSID 或 counter 推導。
2. 啟動 AP 與 `http://192.168.4.1/` 設定頁；配對授權 secret、nonce、嘗試次數與五分鐘 expiry 仍由韌體限制。
3. 電子紙（PhotoPainter 或 GxEPD2）會顯示 `INKTIME PAIRING`、Wi-Fi SSID、AP password、setup URL 與短效 pairing code，因此不必依賴序列埠才能完成配網。
4. 一般畫面只保存 Wi-Fi、密碼、InkTime Server 與旋轉；`192.168.0.50:8765` 會自動
   normalize 為 `http://192.168.0.50:8765`。備援時間／時區與 HTTPS Root CA 收在進階
   設定，且 Root CA 只在 Server 使用 `https://` 時顯示。新自製裝置的 Device Secret
   仍由核准後 claim 一次交付，保存前會再次驗證 URL／CA，且 NVS write/read-back 必須成功。
5. 成功保存後停止 portal、清除 pairing secret／nonce 並重啟；超時或失敗嘗試達上限時進入 bounded sleep。

AP password 是短期配網資訊，不是後端 credential。使用者完成設定後應讓 AP portal 結束，並在管理頁確認 pairing state、credential version 與 delivery mode。

## 診斷錯誤碼

| 錯誤碼 | 含義 | 處理 |
|---|---|---|
| `DEVICE-URL-INVALID` | scheme、host、userinfo 或 fragment 不符合 | 填完整 `https://host[:port]` URL，不要帶帳密。 |
| `DEVICE-TLS-CA-INVALID` | HTTPS 沒有可解析的有效 CA | 重新注入 compile-time CA 或在 portal 貼正確 Root CA PEM。 |
| `DEVICE-TLS-BEGIN` | secure client 初始化失敗 | 先確認 CA、heap 與 server certificate chain；不要改成 `setInsecure()`。 |
| `DEVICE-HTTP-DISALLOWED` | secure build 收到 HTTP | 改用 HTTPS；只有隔離開發 build 才可開 LAN HTTP flag。 |
| `DEVICE-HTTP-PUBLIC-DISALLOWED` | LAN build 收到公開 HTTP host | 改用 HTTPS 或受控私有 host。 |
| `PAIRING-NVS-001` | CA／設定 policy 不合法，或 NVS string／numeric write 失敗 | 修正 CA／欄位內容後重試；不要重啟或改用 `setInsecure()`。 |
| `PAIRING-NVS-002` | NVS namespace 無法開啟 | 檢查裝置儲存狀態與硬體；設定未視為成功。 |
| `PAIRING-NVS-003` | NVS write 後 read-back 不一致 | 保留現有設定，檢查 NVS 容量與 CA 長度後再重試。 |
| `PAIRING-NVS-006` | legacy／journal／clear 後 read-only 檢查失敗 | 保留 canonical A/B／journal，確認 NVS 寫入狀態後重試；不要手動刪除新 journal。 |
| `PAIRING-NVS-007` | commit failure 後 active pointer 無法恢復 | 停止重試與重啟，保留現場 NVS 證據並進入人工 recovery。 |

## 實機驗收邊界

Hosted CI 可以 compile 多個 ESP32 profile、檢查 secure／LAN contract、redirect policy 與 pairing source contract；它不能證明真實 CA chain、板上 Wi-Fi 或實際畫面。PhotoPainter 的 GPIO 與面板命令以官方原始碼為基準鎖定，一般配對／顯示驗收不要求使用者自行探測板上電壓；但若要宣稱 sleep current、refresh peak 或電池續航達標，仍必須使用適當儀器完成實板量測。燒錄後依 [PhotoPainter 指南](WAVESHARE_PHOTOPAINTER_ZH_TW.md) 分開記錄一般功能與功耗證據。
