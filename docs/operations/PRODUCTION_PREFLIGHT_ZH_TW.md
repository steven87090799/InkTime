# Production Preflight

可信任 LAN Production 使用 `.env.lan.production.example`，保持 `INKTIME_ENVIRONMENT=production` 並明確 opt-in HTTP：

```bash
cp .env.lan.production.example .env
# 填入實際 LAN URL、絕對 data/photos 路徑、Git SHA 與 UTC build time
python scripts/production_preflight.py --mode lan --env-file .env
```

成功摘要必須顯示 `transport=trusted-lan-http`、`security_state=degraded`、`tls_enabled=false`、`secure_cookie=false`。Preflight 拒絕公網 HTTP host、placeholder／相對／相同路徑、`simulation_photos`、`local` image tag、HTTP＋Secure Cookie、Proxy Trust 非 0、可寫照片 mount、SQLite 不在 `/data` 與未明確允許的 network filesystem。錯誤只輸出穩定 code、message、fix，不輸出 Secret、Token 或 API Key。

既有 HTTPS Production 仍使用 `.env.production.example` 與 `--mode https`，LAN profile 不會放寬它的 transport 契約。

Production 啟動前會檢查公開 URL、Secure Cookie、受信任 Proxy 數量與 SQLite 掛載點。NFS、CIFS、SMBFS、SSHFS 與 9p 預設拒絕；`INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE=1` 只是可稽核的降級覆寫，並不會停用 flock 或把 WAL 當成網路檔案系統修復方案。

HTTPS 必須搭配 `INKTIME_COOKIE_SECURE=1`。HTTP 必須搭配 `INKTIME_COOKIE_SECURE=0`，且只能透過 `INKTIME_ALLOW_INSECURE_HTTP=1` 明確開啟；Production HTTP 會在 Health/Diagnostics 標示 degraded。`COOKIE_SECURE=1`＋HTTP、HTTP＋未明確允許、Production placeholder 網域、URL 內帳密／路徑／Query／Fragment都會停止啟動並指出修正方式。`INKTIME_PROXY_TRUST` 限制為 0–2，未受信任的轉送標頭不應被代理送入服務。
