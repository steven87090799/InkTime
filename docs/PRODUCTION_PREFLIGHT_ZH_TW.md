# Production Preflight

Production 啟動前會檢查公開 URL、Secure Cookie、受信任 Proxy 數量與 SQLite 掛載點。NFS、CIFS、SMBFS、SSHFS 與 9p 預設拒絕；`INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE=1` 只是可稽核的降級覆寫，並不會停用 flock 或把 WAL 當成網路檔案系統修復方案。

HTTPS 必須搭配 `INKTIME_COOKIE_SECURE=1`。HTTP 僅能透過 `INKTIME_ALLOW_INSECURE_HTTP=1` 明確開啟，Health/Diagnostics 會標示 degraded。`INKTIME_PROXY_TRUST` 限制為 0–2，未受信任的轉送標頭不應被代理送入服務。
