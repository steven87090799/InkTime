# 安全指南

- Web 使用 scrypt；環境不支援時使用 600,000 次 PBKDF2-SHA256。Session 為 HttpOnly／SameSite=Strict，可設定 Secure，預設 30 分鐘。
- 所有 mutation 要求 CSRF；administrator／viewer 在伺服器端授權。登入 IP 15 分鐘內五次失敗會暫時封鎖。
- 路徑使用 `Path.resolve()`／`relative_to()`，拒絕 `..`、URL 重複編碼、絕對路徑、Windows 反斜線、相似前綴與符號連結逃逸。
- Device Token 為高熵隨機值，資料庫只存 HMAC-SHA256；完整值只顯示一次且不進 URL／Log。
- API Key 由部署主密鑰衍生 Fernet 金鑰加密；診斷與 JSON Log 會遞迴遮蔽敏感鍵。
- 舊裝置 API 預設關閉。公網必須 HTTPS、Secure Cookie、防火牆、Proxy 限流與最小權限 Volume。

若主密鑰／`session.key` 遺失，既有 Secret 無法解密；應從備份恢復或重新輸入 Provider Key。疑似 Token 洩漏時立即重新產生並查看最後 IP／連線時間。
