# 安全指南

- Web 使用 scrypt；環境不支援時使用 600,000 次 PBKDF2-SHA256。新密碼需 12–128 字元且保留前後空白。Session 為 HttpOnly／SameSite=Strict，並以 `session_version` 在停用、改密碼或改角色時立即撤銷。User PATCH 會先驗證全部欄位，再於單一 `BEGIN IMMEDIATE` 中更新；no-op 不撤銷 Session，多欄位實際變更只遞增一次版本。
- 系統永遠保留至少一位 `enabled=1` 且角色為 `administrator` 的管理員。停用、降權或兩者並行時，檢查與寫入使用同一 `BEGIN IMMEDIATE`；最後一位管理員更新會以 `last_administrator_required`／409 拒絕。
- 所有 mutation 要求 CSRF；administrator／viewer 在伺服器端授權。登入 IP 15 分鐘內五次失敗會暫時封鎖。
- 路徑使用 `Path.resolve()`／`relative_to()`，拒絕 `..`、URL 重複編碼、絕對路徑、Windows 反斜線、相似前綴與符號連結逃逸。
- Device Token 為高熵隨機值，資料庫只存 HMAC-SHA256；完整值只顯示一次且不進 URL／Log。Release、status 與 Queue manifest/file/ACK 共用同一認證入口；缺少／錯誤／撤銷 Token 回 401，失敗嘗試超限回 429 並帶 `Retry-After`。
- Device Release 由裝置正式指派、Profile latest、有效 Test Assignment 或有效 Queue Item 的單一授權來源判斷；正式指派、latest 與 Queue 必須存在 `published` 的 `releases` row，Test Assignment 明確允許只有受控 Filesystem Release。未授權與不存在一律回 404。目錄與檔案以 dirfd/openat、`O_NOFOLLOW` 逐層開啟；size、SHA-256 與 response 使用同一 descriptor。
- API Key 由部署主密鑰衍生 Fernet 金鑰加密；診斷與 JSON Log 會遞迴遮蔽敏感鍵。
- Webhook 預設只允許 HTTPS，禁止 userinfo、fragment、redirect、private／loopback／link-local／reserved IPv4/IPv6；DNS 驗證後實際連線固定使用同一 IP，TLS SNI 與憑證驗證仍以原 hostname 執行。TCP connect 與 TLS handshake 受 connect timeout 限制；連線後 socket 另套 read timeout。Read timeout 後不換 IP 重送，並關閉 response／connection。內網例外只能由部署者透過 `INKTIME_WEBHOOK_ALLOWLIST` 明確設定 hostname、子網域、IP 或 CIDR。
- JSON API 的 Boolean 只接受 `true`／`false`；不接受字串、0/1、集合或 null。整數拒絕 Boolean、字串與小數；浮點必須有限且在契約範圍內。這會拒絕過去可能被 truthy coercion 接受的模糊輸入。
- CSP 使用每個 response 獨立的密碼學 nonce，`script-src` 不允許 `unsafe-inline`；Production HTTPS 才送出 HSTS。
- 舊裝置 API 預設關閉。Production 預設且建議 HTTPS；明確的 insecure HTTP break-glass 只供受控環境，Health／Preflight 會 degraded，且不得公開至公網。

若主密鑰／`session.key` 遺失，既有 Secret 無法解密；應從備份恢復或重新輸入 Provider Key。疑似 Token 洩漏時立即重新產生並查看最後 IP／連線時間。
