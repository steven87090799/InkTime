# ESP32 自動加入與連線憑證操作契約

本文件對應目前 `esp32/ink-display-7C-photo/ink-display-7C-photo.ino`、`device_config_store_core.h`、`DevicePairingService` 與裝置管理頁。它描述自製 InkTime 韌體的自動加入；不需要人工配對碼；既有 Legacy Token 與 Stock PhotoPainter 相容模式仍保留原契約。

## 0. 目前韌體與 Config Store 版本

- 目前自製 InkTime 韌體版本：`2.8.7`。
- Config Store 目前寫入 schema：`5`。
- Config Store 相容讀取舊 schema：`1`、`2`、`3`、`4`。
- `2.8.2` 將連續 max-awake 故障退避改為 `1h → 6h → 24h → 每日一次`；每次退避後只允許一次 probation，正常完成睡眠或 GPIO4 明確 recovery 會清除故障狀態。此機制不寫 NVS，也不改 TG28／EPD 電源軌。
- `2.8.3` 讓 PhotoPainter 可用嚴格 RFC1918 IP 的 LAN HTTP 直接配對，將 AP 密碼縮為
  每個 session 重新產生的 8 位隨機數字，並簡化手機 Portal；HTTPS CA 驗證、Automatic
  Pairing、Device Secret 與 A/B Config Store 不變。
- Schema 5 可讀取 24-slot payload；PhotoPainter Enhanced 2.8.7 宣告 16 slots/day，舊 schema 最多 12，Server 仍依配對確認的 capability 決定上限，不能只改版本字串。2.8.7 的 KEY1 電源頁停留後驗證 Internal Flash 正式 frame 恢復，見[PhotoPainter 指南](WAVESHARE_PHOTOPAINTER_ZH_TW.md)。
- Schema 4 新增同步策略欄位：`sync_strategy` 與 `sync_time`。`first_display_lead` 沿用既有 `prefetch_lead_minutes`，`sync_time` 必須為空；`fixed_daily` 則必須提供合法的 `HH:MM`。

## 1. 三種裝置認證模式

| 模式 | 後端 `auth_mode` | 認證材料 | 是否進入自動配對 |
|---|---|---|---|
| 自製 InkTime 韌體 | `automatic` | claim／confirm 後取得的長效 `Device Secret`＋`X-InkTime-Credential-Version` | 是 |
| 舊版裝置相容 | `legacy_token` | 既有 Bearer Token | 否；保留現行 Device API 的 Token 相容行為 |
| Stock PhotoPainter | `stock` | 既有 Stock 相容路徑使用的 Bearer Token／伺服器推送 | 否；不得由配對 API 建立 |

管理介面新增自製裝置時不再回傳手動 Token，也不顯示 Token 輸入框。Stock 裝置仍由伺服器主動送到設定的 Stock Host；不要把 Stock 裝置改刷自製 InkTime 自動配對韌體，兩條路徑不可混用。

## 家用內網與對外服務

設定 Wi-Fi 與伺服器網址後，家用內網相框自動加入；不需要抄碼、網站核准或手動 Token。
內網判定沿用 `INKTIME_PUBLIC_URL` 的 RFC1918 IPv4（10/8、172.16/12、192.168/16）
及 `INKTIME_PROXY_TRUST=0`。伺服器自行判定，不信任裝置送來的模式。
公開網址與代理部署仍需管理員在網站按一下核准，但不需要配對碼。
使用者不需要調整另一個註冊開關；名稱、排程與顯示選項可在加入後編輯。
從內網改為公開服務時，尚未完成的內網請求會失效。

## 2. 首次設定與加入流程

1. 在相框設定 Wi-Fi 與 InkTime URL。相框的實體 KEY1 recovery 先於註冊執行，尚未加入也可修改 Wi-Fi。
2. 韌體自動建立持久化 nonce，呼叫原有 request／claim／confirm 協定；使用者不需輸入或複製憑證。
3. 內網 request 直接回 `approved`，請求有效一天；公開網址回 `pending`，有效五分鐘，由網站按一下核准。HTTP 是否允許仍遵守部署政策；公開 Production 使用 HTTPS。
4. claim 驗證 nonce，交付可重試的短效 credential envelope。韌體保存 Device Secret 與版本後 confirm；只有成功 confirm 才建立／啟用正式裝置，避免斷電留下不可用裝置。
5. 升級前的內網 pending 請求可由原有 request 重試或有效 nonce 的 claim 自動完成，不要求重新操作網站。能力衝突仍回明確錯誤，不覆寫既有排程。
6. 正常喚醒沿用已保存憑證取得照片、設定與回報。已撤銷的裝置不會因自動加入重新啟用。

## 相容層

API 路徑、DB／NVS schema 保持相容。`pairing_code=000000` 與 `pairing_mode=trusted_lan`
只是讓現有 sketch 通過回應檢查並選擇免抄碼畫面的 wire 常數，沒有授權作用。
`enrollment_mode` 才說明伺服器決定的 `automatic` 或 `administrator` 政策；nonce、
實際存於伺服器的模式、管理員權限與 confirm 憑證決定是否准許加入。
既有 `pairing_code_hash`／attempts 欄位保留供舊資料讀取，新流程不產生配對碼雜湊、
不驗證人工碼、不累計核准碼錯誤。既有 Legacy Token 仍可連線；網站不再提供複製／重生 Token 的操作。

## 3. API 摘要

### 裝置公開配對端點

`POST /api/device/v1/pairing/request`

```json
{
  "device_id": "esp32-ABC123",
  "pairing_nonce": "高熵隨機字串",
  "firmware_identity": "ESP32-S3-PhotoPainter",
  "firmware_version": "2.8.7",
  "panel_profile": "safe_4c",
  "capabilities": {
    "automatic_pairing": true,
    "ab_credential_store": true
  }
}
```

成功回 `201`；同一 nonce 重試回 `200` 與原有 request ID。Response 含 `status`、`pairing_id`、`device_id`、`enrollment_mode`、`expires_in_seconds`、`server_epoch`、`poll_after_seconds=3` 及上述相容常數，不含 Device Secret。內網回 `approved`，TTL 最多 86400 秒；公開回 `pending`，TTL 最多 300 秒。未知欄位、非 object JSON、非法 ID／型別等仍拒絕。

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
- `POST /api/v1/device-pairing/{pairing_id}/approve`：管理員 Session 與 CSRF，Body 可為 `{}`；不需配對碼。舊 Client 的 `pairing_code` 欄位僅為相容而接受。內網不需要使用此端點。
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

Automatic credential 缺少版本、版本不符、裝置未處於 `paired`、已停用或 hash 不符時都 fail-closed。此流程不做 graceful rotation；管理員撤銷後 current Secret 立即失效，只有短效、單次 repair permission 允許「重新配對 → 新 Secret」。撤銷狀態本身仍拒絕所有一般裝置端點。

裝置收到一般 authenticated endpoint 的 `401` 或 `403` 時：

- 不重試同一個 Secret、不繼續下載檔案、不送 Status 造成更多失敗；
- 將 `auth_state` 寫成 `auth_invalid` 或 `revoked`，保留錯誤碼與 bounded recovery wake；
- 由管理員按「重新連線」允許該裝置重新取得憑證。下一輪使用同一 device_id，保留管理員設定；內網自動完成，公開網址按一下核准，均不需配對碼。

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
| `DEVICE-PAIRING-TIMEOUT`、`DEVICE-PAIRING-EXPIRED` | 保留或清除正確的 pairing state，已有待處理請求時每分鐘重試；建立請求失敗則進入 1 分鐘、5 分鐘、15 分鐘、1 小時 bounded deep-sleep backoff；只有未配對裝置或明確 repair permission 才建立新 request。 |
| `PAIR-002` | pairing request 受到 IP／Device bounded rate limit，等待 `Retry-After`。 |
| `PAIR-009` | claim 已消費；不可要求 Server 再顯示同一 Secret。 |
| `DEVICE-AUTH-INVALID`／`DEVICE-AUTH-REVOKED` | 檢查管理員是否已 enable repair；不要在 AP 頁貼入新 Token。 |
| `PAIRING-NVS-004` 到 `PAIRING-NVS-007` | NVS slot、pointer、journal 或 read-back 失敗；裝置停止網路工作，先保留最後有效設定並依硬體 reset 流程處理。 |

加入請求過期後，裝置會進入 bounded deep sleep，不會保持 Wi-Fi 或永久開啟 AP。正常網路、韌體 compile、PhotoPainter BUSY／面板顏色／orientation／ghosting、PMIC／GPIO5／深度睡眠與真實 NAS／LAN 的驗收必須分開記錄；hosted CI 通過不等於實機通過。
