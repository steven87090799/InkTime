# ESP32 自動配對與 Device Secret 操作契約

本文件對應目前 `esp32/ink-display-7C-photo/ink-display-7C-photo.ino`、`device_config_store_core.h`、`DevicePairingService` 與裝置管理頁。它只描述自製 InkTime 韌體的自動配對；既有 Legacy Token 與 Stock PhotoPainter 相容模式仍保留原契約。

## 0. 目前韌體與 Config Store 版本

- 目前自製 InkTime 韌體版本：`2.8.6`。
- Config Store 目前寫入 schema：`5`。
- Config Store 相容讀取舊 schema：`1`、`2`、`3`、`4`。
- `2.8.2` 將連續 max-awake 故障退避改為 `1h → 6h → 24h → 每日一次`；每次退避後只允許一次 probation，正常完成睡眠或 GPIO4 明確 recovery 會清除故障狀態。此機制不寫 NVS，也不改 TG28／EPD 電源軌。
- `2.8.3` 讓 PhotoPainter 可用嚴格 RFC1918 IP 的 LAN HTTP 直接配對，將 AP 密碼縮為
  每個 session 重新產生的 8 位隨機數字，並簡化手機 Portal；HTTPS CA 驗證、Automatic
  Pairing、Device Secret 與 A/B Config Store 不變。
- Schema 5 將新能力容量提高至 24 slots；舊 schema 最多 12，Server 仍依配對確認的 capability 決定上限，不能只改版本字串。2.8.6 另加入 KEY1 電源頁停留後驗證 SD 原圖恢復，見[PhotoPainter 指南](WAVESHARE_PHOTOPAINTER_ZH_TW.md)。
- Schema 4 新增同步策略欄位：`sync_strategy` 與 `sync_time`。`first_display_lead` 沿用既有 `prefetch_lead_minutes`，`sync_time` 必須為空；`fixed_daily` 則必須提供合法的 `HH:MM`。

## 1. 三種裝置認證模式

| 模式 | 後端 `auth_mode` | 認證材料 | 是否進入自動配對 |
|---|---|---|---|
| 自製 InkTime 韌體 | `automatic` | claim／confirm 後取得的長效 `Device Secret`＋`X-InkTime-Credential-Version` | 是 |
| 舊版裝置相容 | `legacy_token` | 既有 Bearer Token | 否；保留現行 Device API 的 Token 相容行為 |
| Stock PhotoPainter | `stock` | 既有 Stock 相容路徑使用的 Bearer Token／伺服器推送 | 否；不得由配對 API 建立 |

管理介面新增自製裝置時不再回傳手動 Token，也不顯示 Token 輸入框。Stock 裝置仍由伺服器主動送到設定的 Stock Host；不要把 Stock 裝置改刷自製 InkTime 自動配對韌體，兩條路徑不可混用。

## 2. 首次設定與配對流程

1. 新自製裝置不必先按「新增裝置」：第一次 pairing request 只建立待處理 enrollment，不建立或啟用 `devices` 正式資料列。管理員可在 `/devices` 的待處理列輸入實體相框顯示的配對碼，並設定名稱、面板 Profile、交付模式、時區與排程；頁面不回傳 Secret 或配對碼。
2. ESP32 進入 AP 設定頁，只填 Wi-Fi、Backend Origin 與 TLS Root CA。既有 `device_token` 若存在會保留為 Legacy 相容資料，但不再提供手動輸入欄位。
3. ESP32 先把新的高熵 `pairing_nonce` 寫入 A/B Config，再將唯一 `device_id` 與該 nonce POST 到 `/api/device/v1/pairing/request`。若 request 已送達但 response 在掉電中遺失，下一次喚醒會重用同一 nonce 取回原 enrollment metadata；production 預設使用 HTTPS；明確允許的可信任 LAN production 可用 HTTP，Body 必須是精確的 `application/json` object。
4. Server 只保存 nonce HMAC 與配對碼 HMAC；配對碼短效 5 分鐘，且錯誤核准最多 5 次。Response 的六位數碼只供裝置顯示在 pairing screen（PhotoPainter 或 GxEPD2），不能寫入 Log、URL、DOM、Audit 或可逆欄位。
5. 管理員在 `/devices` 的待處理表輸入實體相框上的六位數配對碼，再核對裝置名稱、Firmware、Profile、Capabilities 並按「核准」。管理頁永遠不顯示伺服器已知的配對碼；核准 API 是管理員 Session，仍需要 CSRF；Viewer 不可核准、拒絕、撤銷或允許重新配對。
6. ESP32 每 3 秒 POST `/api/device/v1/pairing/claim`，單次喚醒最多輪詢約 30 秒。未核准回 `202` 並附 `Retry-After: 3`；核准後回傳可重試的短效 credential envelope 內容。ESP32 若在 claim 或 confirm 中斷，下一次喚醒會以同一 `pairing_id`、nonce 取回相同 credential，不建立新的 request。
7. ESP32 先把 Secret、Device ID、`auth_state=credential_issued` 與 Credential Version 寫入現有 A/B `ConfigPayload`，用 `/api/device/v1/pairing/confirm` 以 Bearer Secret 與版本 Header 完成 confirm；只有 confirm 成功後才清除 pairing state、寫成 `paired`，Server 才建立／啟用正式 device row。Secret 不會由 Server 顯示，也不會從韌體 Log 讀出。

裝置正常喚醒時不會重新 request 或 claim；只會以 NVS 中的 Secret 加上版本 Header 呼叫既有 Manifest、Queue、檔案、Offline Schedule 與 Status API。

## 3. API 摘要

### 裝置公開配對端點

`POST /api/device/v1/pairing/request`

```json
{
  "device_id": "esp32-ABC123",
  "pairing_nonce": "高熵隨機字串",
  "firmware_identity": "ESP32-S3-PhotoPainter",
  "firmware_version": "2.8.6",
  "panel_profile": "safe_4c",
  "capabilities": {
    "automatic_pairing": true,
    "ab_credential_store": true
  }
}
```

成功回 `201`，只在這個 ESP32 request response 包含 `pairing_id`、`device_id`、`pairing_code`、`expires_in_seconds=300` 與 `poll_after_seconds=3`。不包含 Device Secret；重試同一 request 只回相同 enrollment metadata，不再回配對碼。未知欄位、非 object JSON、非 JSON Content-Type、過大 Body、非法 ID／型別都拒絕。

`POST /api/device/v1/pairing/claim`

```json
{"pairing_id":"…","pairing_nonce":"…"}
```

狀態為 pending 時回 `202`；核准後回 `200` 並在短效 envelope 仍有效時重試回傳相同 `device_secret` 與版本。confirm 前不建立正式 device row；confirm 後再 claim 回 `409`，過期回 `410`，錯誤 nonce 會增加 bounded claim attempts，不會洩漏配對狀態以外的 Secret。

`POST /api/device/v1/pairing/confirm`

```json
{
  "pairing_id": "…",
  "device_id": "esp32-ABC123",
  "pairing_nonce": "高熵隨機字串"
}
```

此 Body 不含 Secret；Request Header 必須是 `Authorization: Bearer ids_…` 與 `X-InkTime-Credential-Version`。Server 會驗證 nonce、Secret、版本與 envelope，成功回 `confirmed`；同一有效 confirm 重送回 `already_confirmed`。

### 管理端點

- `GET /api/v1/device-pairing/pending`：只回傳尚未完成的配對請求、設定與嘗試次數；不回傳配對碼、nonce、hash、envelope 或 Device Secret。
- `POST /api/v1/device-pairing/{pairing_id}/approve`：Body 為管理員輸入的 `{"pairing_code":"六位數","device_config":{...}}`；Server 只做 constant-time HMAC compare，核准後才可 claim。
- `POST /api/v1/device-pairing/{pairing_id}/reject`：拒絕請求並讓裝置回到未配對狀態。
- `POST /api/v1/devices/{device_id}/revoke-credential`：讓目前 Secret 立即失效；Stock 與 Legacy 不可用這個自動 credential 操作。
- `POST /api/v1/devices/{device_id}/enable-repair`：管理員允許 automatic 裝置重新配對；下一次裝置喚醒會建立新的短效請求。

裝置在本地標記 `auth_invalid`／`revoked` 後，只有在下一輪先以現有 credential 呼叫 `GET /api/device/v1/pairing/repair-permission` 並取得 `pairing_allowed`，才會建立新的 pairing request；一般 paired wake 不呼叫此 probe。

所有管理端點都受登入、administrator role 與 CSRF 保護；配對 response 與 error response 使用 `Cache-Control: no-store`。production pairing 預設只接受 HTTPS；只有明確啟用 `INKTIME_ALLOW_INSECURE_HTTP=1` 的可信任 LAN production 才接受 HTTP。

## 4. Credential 生命週期

Server 只在 `devices.device_secret_hash` 保存 HMAC hash，不保存明文 Secret。每次新的 claim credential 使 `credential_version` 加一；裝置對一般 API 加上：

```http
Authorization: Bearer ids_…
X-InkTime-Credential-Version: 2
```

Automatic credential 缺少版本、版本不符、裝置未處於 `paired`、已停用或 hash 不符時都 fail-closed。本 PR 不做 graceful rotation；管理員撤銷後 current Secret 立即失效，只有短效、單次 repair permission 允許「重新配對 → 新 Secret」。撤銷狀態本身仍拒絕所有一般裝置端點。

裝置收到一般 authenticated endpoint 的 `401` 或 `403` 時：

- 不重試同一個 Secret、不繼續下載檔案、不送 Status 造成更多失敗；
- 將 `auth_state` 寫成 `auth_invalid` 或 `revoked`，保留錯誤碼與 bounded recovery wake；
- 只有管理員先按「允許重新配對」後，下一輪才會用同一 `device_id` 建立新的配對 request；仍需新的配對碼與人工核准。

## 5. NVS、清除與既有功能保護

新增欄位全部位於現有 A/B `ConfigPayload`：

- `device_secret`、`device_id`、`auth_state`、`credential_version`；
- `pairing_id`、`pairing_nonce`、`pairing_expires_at_epoch`、`pairing_retry_at_epoch`、`pairing_retry_attempt`；
- Config Store 目前寫入 payload schema `5`；deserialize 相容讀取舊 schema `1`、`2`、`3`、`4`；
- schema 4 的 `sync_strategy` 支援 `first_display_lead` 與 `fixed_daily`；前者使用既有 `prefetch_lead_minutes` 且 `sync_time` 為空，後者要求合法 `HH:MM` 的 `sync_time`；
- Config Store 仍執行 slot CRC、generation、pointer recovery、prepare／commit read-back；
- 不新增散落的明文 `Preferences` credential key；Factory Reset 會清除新的正式 A/B payload 與既有 legacy 設定。

這些契約不得被自動配對改壞：

- Stock `/dataUP` 與伺服器主動推送不會呼叫 pairing request／claim。
- 舊 `device_token` 可繼續通過 Legacy API；自製韌體只有在沒有 Secret／Token 且非 Stock 時才走首次配對。
- `button_wake_action=local_next`、Offline Schedule、Manifest、Queue ACK、TLS trust anchor、Deep Sleep 與 GPIO hold 的既有 fail-closed 行為維持不變。
- pairing request、claim、approve、revoke、repair 的 audit／device event 只記狀態與識別資料，不記 pairing code、nonce、Bearer 或 Device Secret。

## 6. 故障處理

| 現象／錯誤碼 | 處理 |
|---|---|
| `DEVICE-PAIRING-TIMEOUT`、`DEVICE-PAIRING-EXPIRED` | 保留或清除正確的 pairing state，進入 1 分鐘、5 分鐘、15 分鐘、1 小時 bounded deep-sleep backoff；只有未配對裝置或明確 repair permission 才建立新 request。 |
| `PAIR-002` | pairing request 受到 IP／Device bounded rate limit，等待 `Retry-After`。 |
| `PAIR-006` | 配對碼錯誤次數用盡；拒絕該 request，再建立新的 request。 |
| `PAIR-009` | claim 已消費；不可要求 Server 再顯示同一 Secret。 |
| `DEVICE-AUTH-INVALID`／`DEVICE-AUTH-REVOKED` | 檢查管理員是否已 enable repair；不要在 AP 頁貼入新 Token。 |
| `PAIRING-NVS-004` 到 `PAIRING-NVS-007` | NVS slot、pointer、journal 或 read-back 失敗；裝置停止網路工作，先保留最後有效設定並依硬體 reset 流程處理。 |

配對碼過期後，裝置會進入 bounded deep sleep，不會保持 Wi-Fi 或永久開啟 AP。正常網路、韌體 compile、PhotoPainter BUSY／面板顏色／orientation／ghosting、PMIC／GPIO5／深度睡眠與真實 NAS／LAN 的驗收必須分開記錄；hosted CI 通過不等於實機通過。
