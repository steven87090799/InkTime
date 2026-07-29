# 安全指南

- Web 使用 scrypt；環境不支援時使用 600,000 次 PBKDF2-SHA256。新密碼需 12–128 字元且保留前後空白。Session 為 HttpOnly／SameSite=Strict，並以 `session_version` 在停用、改密碼或改角色時立即撤銷。
- 所有 mutation 要求 CSRF；administrator／viewer 在伺服器端授權。登入 IP 15 分鐘內五次失敗會暫時封鎖。
- 路徑使用 `Path.resolve()`／`relative_to()`，拒絕 `..`、URL 重複編碼、絕對路徑、Windows 反斜線、相似前綴與符號連結逃逸。
- Device Token 為高熵隨機值，資料庫只存 HMAC-SHA256；完整值只顯示一次且不進 URL／Log。
- Device Release 由裝置正式指派、Profile latest、有效 Test Assignment 或有效 Queue Item 的單一授權來源判斷；未授權與不存在一律回 404，檔案使用同一已驗證 descriptor 讀取與雜湊。
- API Key 由部署主密鑰衍生 Fernet 金鑰加密；診斷與 JSON Log 會遞迴遮蔽敏感鍵。
- Webhook 預設只允許 HTTPS，禁止 userinfo、fragment、redirect、private／loopback／link-local／reserved IPv4/IPv6；DNS 驗證後實際連線固定使用同一 IP，TLS SNI 與憑證驗證仍以原 hostname 執行。內網例外只能由部署者透過 `INKTIME_WEBHOOK_ALLOWLIST` 明確設定 hostname、子網域、IP 或 CIDR。
- CSP 使用每個 response 獨立的密碼學 nonce，`script-src` 不允許 `unsafe-inline`；Production HTTPS 才送出 HSTS。
- 舊裝置 API 預設關閉。公網必須 HTTPS、Secure Cookie、防火牆、Proxy 限流與最小權限 Volume。

若主密鑰／`session.key` 遺失，既有 Secret 無法解密；應從備份恢復或重新輸入 Provider Key。疑似 Token 洩漏時立即重新產生並查看最後 IP／連線時間。
