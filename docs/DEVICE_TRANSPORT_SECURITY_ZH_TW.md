# 裝置傳輸安全

## 契約

裝置 API 保留 `Authorization: Bearer <device-token>`。伺服器只保存加 pepper 的 HMAC hash；明文 Token 只在建立或重生時顯示一次。驗證失敗依來源 HMAC 雜湊做五分鐘、20 次的有界 rate limit；停用裝置立即拒絕。

Manifest 與 BIN 必須符合 Token 所屬裝置 Profile。BIN endpoint 重新檢查 Manifest 是否列出檔案、大小與 SHA-256；Token、完整 NAS 路徑不進 Log、Device Event 或診斷包。

## HTTP 與 HTTPS

Bearer Token 是身分驗證，不是加密。HTTP 會讓 Token 以明文經過網路，只允許在隔離 IoT VLAN 使用，並應啟用 client isolation、防火牆限制裝置只能連 InkTime Server，禁止跨網路路由。

目前正式韌體沒有完整 Web CA provisioning；因此未配置可信 CA 的 HTTPS 預設拒絕，且沒有 `WiFiClientSecure::setInsecure()`。`INKTIME_ALLOW_UNVERIFIED_HTTPS` 只供開發編譯、預設關閉並輸出警告，不能作正式部署。加入 `WiFiClientSecure` 可信 CA／憑證輪替前，跨網路部署使用 VPN 或 TLS 終端內的受控 IoT 網段。

## 裝置測試 ACK

狀態依序為 `assigned → manifest_fetched → payload_downloaded → payload_verified → display_confirmed → consumed`。中斷可重試；24 小時或五次下載後 expired。只有相同 Release、`payload_sha256_verified=true`、`display_updated=true`、無錯誤與相容 Profile 的 `/status` 會 consumed。
