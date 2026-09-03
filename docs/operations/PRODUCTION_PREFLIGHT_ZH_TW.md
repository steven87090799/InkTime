# Production Preflight

本頁說明 CLI／啟動期檢查的分工；NAS 正式操作使用[NAS 更新器](NAS_TAG_DEPLOYMENT_ZH_TW.md)的 `.env.nas`／部署契約流程，不能只跑 preflight 就省略 recovery point。

可信任 LAN Production 使用 `.env.lan.production.example`，保持 `INKTIME_ENVIRONMENT=production` 並明確 opt-in HTTP：

```bash
cp .env.lan.production.example .env
# 填入實際 LAN URL、絕對 data/photos 路徑、Git SHA 與 UTC build time
python scripts/production_preflight.py --mode lan --env-file .env
```

啟動前摘要必須顯示 `validation_scope=prestart-config`、`runtime_mount_validation=deferred-to-container-startup`、`transport=trusted-lan-http`、`security_state=degraded`、`tls_enabled=false`、`secure_cookie=false`。這一階段拒絕公網 HTTP host、placeholder／相對／相同路徑、`simulation_photos`、`local` image tag、HTTP＋Secure Cookie、Proxy Trust 非 0，以及 Compose 未宣告唯讀 `/photos`；它不會在容器啟動前假稱 host runner 已有容器內 `/photos` mount。錯誤只輸出穩定 code、message、fix，不輸出 Secret、Token 或 API Key。

既有 HTTPS Production 仍使用 `.env.production.example` 與 `--mode https`，LAN profile 不會放寬它的 transport 契約。

Production 啟動前 CLI 會檢查公開 URL、Secure Cookie、受信任 Proxy、host path 與 Compose 宣告；容器啟動時的 application preflight 才以實際 mountinfo 驗證精確唯讀 `/photos`、可寫 nested mount 與 SQLite filesystem。NFS、CIFS、SMBFS、SSHFS 與 9p 預設拒絕；`INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE=1` 只是可稽核的降級覆寫，並不會停用 flock 或把 WAL 當成網路檔案系統修復方案。

HTTPS 必須搭配 `INKTIME_COOKIE_SECURE=1`。HTTP 必須搭配 `INKTIME_COOKIE_SECURE=0`，且只能透過 `INKTIME_ALLOW_INSECURE_HTTP=1` 明確開啟；Production HTTP 會在 Health/Diagnostics 標示 degraded。`COOKIE_SECURE=1`＋HTTP、HTTP＋未明確允許、Production placeholder 網域、URL 內帳密／路徑／Query／Fragment都會停止啟動並指出修正方式。`INKTIME_PROXY_TRUST` 限制為 0–2，未受信任的轉送標頭不應被代理送入服務。
