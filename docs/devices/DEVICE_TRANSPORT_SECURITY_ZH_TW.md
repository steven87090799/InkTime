# 裝置傳輸安全

## 契約

裝置 API 依模式接受自動配對的 `Authorization: Bearer <device-secret>` 加 `X-InkTime-Credential-Version`，或既有 Legacy／Stock 相容的 `Authorization: Bearer <device-token>`。伺服器只保存加 pepper 的 HMAC hash；claim 只以短效加密 envelope 交付 credential，允許同一 pairing retry 取回相同值，confirm 後不再保存明文。Legacy Token 只在建立或重生時顯示一次。驗證失敗依來源 HMAC 雜湊做五分鐘、20 次的有界 rate limit；停用或撤銷裝置立即拒絕。

Manifest 與 BIN 必須符合 credential 所屬裝置 Profile。BIN endpoint 重新檢查 Manifest 是否列出檔案、大小與 SHA-256；Token、Device Secret、完整 NAS 路徑不進 Log、Device Event 或診斷包。

## HTTP 與 HTTPS

Device Secret／Legacy Bearer Token 是身分驗證，不是加密。HTTP 會讓 credential 以明文經過網路，只允許在隔離 IoT VLAN 使用，並應啟用 client isolation、防火牆限制裝置只能連 InkTime Server，禁止跨網路路由。

PhotoPainter 可直接連 literal RFC1918 IPv4 HTTP；公開 IP、hostname、loopback、link-local
與 IPv6 HTTP 都拒絕。HTTPS trust anchor 可由 compile-time CA 或 Portal「進階設定」提供，
未配置可信 CA 時預設拒絕，且沒有 `WiFiClientSecure::setInsecure()`。跨網路部署仍須使用
有可驗證 CA 的 HTTPS、VPN 或受控 IoT VLAN。

## 裝置測試 ACK

狀態依序為 `assigned → manifest_fetched → payload_downloaded → payload_verified → display_confirmed → consumed`。中斷可重試；24 小時或五次下載後 expired。只有相同 Release、`payload_sha256_verified=true`、`display_updated=true`、無錯誤與相容 Profile 的 `/status` 會 consumed。
