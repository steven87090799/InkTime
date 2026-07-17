# Docker 與 NAS 部署指南

Compose 提供 web、worker、scheduler 三服務，共用 `/data` 與唯讀 `/photos`。映像以 UID 10001 非 root 執行、root filesystem 唯讀、`/tmp` 使用 tmpfs、啟用 Healthcheck、Log Rotation 與資源限制範例。

```bash
cp .env.example .env
sudo chown -R 10001:10001 /volume1/docker/inktime
INKTIME_DATA_PATH=/volume1/docker/inktime \
INKTIME_PHOTO_PATH=/volume1/photo \
docker compose up -d --build
```

Synology／QNAP 請確認容器可讀照片 ACL，資料 Volume 位於本機可靠磁碟，不建議把活躍 SQLite 放在不支援鎖定的網路檔案系統。反向代理啟用 HTTPS 後設定 Secure Cookie。更新前備份，更新後檢查 `/health/ready` 與三服務 Log。
