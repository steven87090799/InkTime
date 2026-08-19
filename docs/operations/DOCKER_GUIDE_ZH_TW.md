# InkTime Docker 部署規格（Intel N100）

OpenAI Batch 正式操作與故障排除另見 [OpenAI Batch 照片分析指南](../OPENAI_BATCH_ANALYSIS_ZH_TW.md)。`/data/batches` 必須與 SQLite 一起掛載持久化；Batch JSONL 不使用 `/tmp`，照片掛載仍保持唯讀。

這份文件是正式部署契約。日常分析、排程、裝置、備份與 Log 層級都從 Web 管理；只有主機路徑、Port、映像版本、HTTPS 與容器 CPU／記憶體上限需要在容器外設定。

Intel N100 為 4 核心／4 執行緒、最高 3.4 GHz、6 W Processor Base Power；對 InkTime 的單機 Web、SQLite、低並行圖片處理足夠。6 W 是處理器規格，不等於整台迷你主機插座功耗。[Intel N100 官方規格](https://www.intel.com/content/www/us/en/products/compare.html?productIds=88183%2C231803)

## 1. 主機與儲存規格

| 項目 | 最低 | 建議 |
|---|---:|---:|
| CPU | x86-64 2 核 | Intel N100 4C/4T |
| RAM | 4 GiB | 8 GiB；大量 24MP／HEIF 圖片建議 16 GiB |
| 系統碟 | 10 GiB 可用 | SSD，另預留縮圖、發布與備份空間 |
| Docker | Engine 24+ | 最新穩定版＋Compose v2 |
| 檔案系統 | 支援 POSIX lock | ext4／xfs／btrfs；SQLite 資料庫放本機 SSD |
| 網路 | ESP32 可連到 TCP 8765 | HTTPS 反向代理或隔離 LAN |

`/photos` 可以是 NAS 掛載，但 `/data/inktime.db` 不應放在不保證檔案鎖與 fsync 語意的 SMB／NFS 遠端分享。照片 Volume 是唯讀；資料 Volume 必須讓容器 UID/GID `10001:10001` 可寫。

## 2. 服務與 N100 預設上限

| 服務 | 用途 | CPU 上限 | 記憶體上限 | 待機行為 |
|---|---|---:|---:|---|
| `inktime-web` | Web、API、ESP32 下載 | 0.75 CPU | 384 MiB | 1 Gunicorn worker × 2 threads，無 HTTP access log |
| `inktime-worker` | 掃描、特徵、模型、渲染 | 2.0 CPU | 1 GiB | 無工作時預設每 15 秒檢查一次 |
| `inktime-scheduler` | 備份、租約回收 | 0.25 CPU | 192 MiB | 預設每 60 秒檢查一次 |

上限不是預先保留量。閒置時容器只保留 Python 程序與必要頁面，不會主動占滿設定值。若 Worker 因超大或損壞圖片觸發 OOM，先在 Web 將 `analysis.concurrency=1`、`worker.queue_multiplier=1`，再把 `INKTIME_WORKER_MEMORY` 提高到 `1536m`；不要先無限制提高並行。

## 3. 首次部署：先選 LAN HTTP 或 Production HTTPS

```bash
git clone <你的 InkTime 私有儲存庫 URL> InkTime
cd InkTime
mkdir -p data
sudo chown -R 10001:10001 data
```

### 3.1 可信任 LAN Production HTTP

```bash
cp .env.lan.production.example .env
```

把 `INKTIME_PUBLIC_URL` 改成瀏覽器實際使用的 RFC1918 IP、`.local` 或單標籤內網主機；設定不同的絕對資料／唯讀照片路徑，並用實際 Git SHA 與 UTC build time 取代 `CHANGE_ME`。此模式固定搭配：

```dotenv
INKTIME_ENVIRONMENT=production
INKTIME_COOKIE_SECURE=0
INKTIME_ALLOW_INSECURE_HTTP=1
INKTIME_PROXY_TRUST=0
```

啟動前執行 `python scripts/production_preflight.py --mode lan --env-file .env`。它會檢查 transport 組合、LAN host、placeholder／相對路徑、唯讀 photos mount、SQLite `/data`、network filesystem opt-in 與 immutable image identity，失敗時輸出穩定錯誤碼及修正方式。成功後 Health／diagnostics 仍會明確顯示 `environment=production`、`transport=trusted-lan-http`、`security_state=degraded`、`tls_enabled=false`、`secure_cookie=false`。此模式不可直接公開至 Internet；`.env.local.example` 只供 development／模擬。

### 3.2 正式 HTTPS Reverse Proxy

```bash
cp .env.production.example .env
```

必須先把 `https://inktime.example.com` 改成實際網域；Production 會拒絕 localhost、`.invalid`、`example.com`／子網域、URL 內帳密與帶路徑 URL。公開部署預設且建議固定搭配：

```dotenv
INKTIME_ENVIRONMENT=production
INKTIME_PUBLIC_URL=https://inktime.your-domain.example
INKTIME_COOKIE_SECURE=1
INKTIME_ALLOW_INSECURE_HTTP=0
INKTIME_PROXY_TRUST=1
```

`INKTIME_PROXY_TRUST` 必須等於實際受信任 Proxy hop 數，不可為了「讓它能跑」任意放大。

### 3.3 既有 Production HTTP break-glass 相容邊界

Production 在特殊、受控且不連接公網的測試環境，可明確設定：

```dotenv
INKTIME_ENVIRONMENT=production
INKTIME_PUBLIC_URL=http://受控主機:8765
INKTIME_COOKIE_SECURE=0
INKTIME_ALLOW_INSECURE_HTTP=1
INKTIME_PROXY_TRUST=0
```

新 LAN 部署請用 3.1 的專用範例與 preflight。這段只說明既有環境的相容邊界：Health／Preflight 會標示 `degraded`；Secure Cookie、HSTS 與 TLS 安全保證均不成立，不可透過 Internet 使用。系統不會自動從 HTTPS fallback 到此模式。

不要把 `.env`、資料庫、`session.key`、API Key、Device Secret 或 Legacy 裝置 Token Commit。啟動：

```bash
docker compose config --quiet
scripts/build_release_image.sh
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8765/health/ready
```

三個服務都應顯示 `healthy`。首次管理員帳號需 3–64 個 ASCII 識別字元，密碼需 12–128 字元；密碼前後空白屬於密碼內容，不會被自動裁切。

## 4. `.env` 部署欄位

| 欄位 | 預設 | 說明 |
|---|---|---|
| `INKTIME_PORT` | `8765` | 主機對外 Port |
| `INKTIME_DATA_PATH` | 範例要求絕對路徑 | SQLite、快取、字型、備份、發布；正式模式可寫且資料庫位於容器 `/data` |
| `INKTIME_PHOTO_PATH` | 範例要求絕對路徑 | 原始照片；容器內固定 `/photos` 且唯讀。正式模式拒絕 `simulation_photos` 與 placeholder |
| `INKTIME_ENVIRONMENT` | local 範例為 `development` | LAN／HTTPS 正式部署都必須為 `production` |
| `INKTIME_PUBLIC_URL` | `http://localhost:8765` | 瀏覽器實際使用的 Origin；不可含帳密、路徑、Query 或 Fragment |
| `INKTIME_COOKIE_SECURE` | local `0`／production 建議 `1` | 必須和 HTTP／HTTPS 模式一致；break-glass HTTP 為 `0` |
| `INKTIME_ALLOW_INSECURE_HTTP` | local `1`／production 建議 `0` | HTTP 的明確降級開關，不會自動 fallback；Production 啟用時 health degraded |
| `INKTIME_PROXY_TRUST` | local `0` | 只信任實際存在的 Proxy hop，正式單層 Proxy 通常為 `1` |
| `INKTIME_WEBHOOK_ALLOWLIST` | 空 | 僅在確實需要內網 Webhook 時填精確 hostname、`.example.com` 子網域、IP 或 CIDR；一般公開端點不需設定 |
| `INKTIME_ACCESS_LOG` | `0` | 是否逐一輸出 HTTP request；正式環境維持關閉 |
| `INKTIME_ENABLE_LEGACY_WEBUI` | `false` | 舊 `/review`、`/sim` 與相關 API；正式、開發、測試皆須明確設為 `true` 才開啟，且仍受平台登入／權限保護 |
| `INKTIME_LOG_LEVEL` | `INFO` | 資料庫尚未初始化前的 bootstrap 層級；之後從 Web 控制 |
| `INKTIME_LOG_MAX_SIZE` | `5m` | 每個 Docker Log 檔上限 |
| `INKTIME_LOG_MAX_FILES` | `3` | 每個服務保留檔數；三服務預設總上限約 45 MiB |
| `INKTIME_WEB_CPUS`／`MEMORY` | `0.75`／`384m` | Web cgroup 上限 |
| `INKTIME_WORKER_CPUS`／`MEMORY` | `2.0`／`1g` | 圖片 Worker cgroup 上限 |
| `INKTIME_SCHEDULER_CPUS`／`MEMORY` | `0.25`／`192m` | Scheduler cgroup 上限 |
| `INKTIME_WEB_WORKERS`／`THREADS` | `1`／`2` | N100 低記憶體 Web 拓撲 |

Compose 還啟用非 root、唯讀 root filesystem、`no-new-privileges`、PID 上限、獨立 tmpfs、優雅停止與 `unless-stopped`。

## 5. Web 端完成日常設定

首次登入後依序操作：

1. 「設定」：確認 `analysis.concurrency=1`、`worker.queue_multiplier=1`、`worker.poll_seconds=15`、`scheduler.poll_seconds=60`。
2. 「模型」：新增 Provider、API Key、模型與逾時；先測試連線。
3. 「維護」：以容器路徑 `/photos` 建立掃描工作。
4. 「工作」：先用 10～100 張與小額預算驗證，再增加數量。
5. 「渲染」：預覽並選擇內建手寫／文青繁中字型，或上傳自訂字型，再發布 480×800 版本。
6. 「裝置」：設定 ESP32 的時區／每日刷新／旋轉；新自製板由自動配對取得一次性 Device Secret，既有 Legacy 裝置才使用相容 Token。
7. 「設定」：選擇 `INFO`／`WARNING`／`ERROR` Log 層級與自動備份保留數。
8. 「診斷」：確認 Web RSS、cgroup 記憶體、Queue、WAL、照片掛載與版本。

日常操作不需要改 Python。容器內程式不能安全地改寫宿主機 Volume、Port、cgroup 或 Docker logging driver，因此這些少量部署欄位保留在 `.env`。

## 6. Log、健康與故障檢查

```bash
docker compose logs --since=30m inktime-web
docker compose logs --since=30m inktime-worker
docker compose logs --since=30m inktime-scheduler
docker compose logs --since=30m | grep -E '"level":"(warning|error|critical)"'
docker stats --no-stream
```

應用只輸出啟動、工作開始、節流後的進度、完成、取樣錯誤、備份與 ESP32 狀態；預設不輸出每個健康檢查、HTTP request 或每張成功照片。完整規則見 [Log 與問題追蹤指南](LOGGING_GUIDE_ZH_TW.md)。

健康端點：

- `/health/live`：Web 程序可回應。
- `/health/ready`：Migration、SQLite、發布目錄、設定與工作租約正常。
- `/health/detail`：登入管理員後查看詳細版本。
- Worker／Scheduler：Compose healthcheck 確認目標程序仍存在。

## 7. HTTPS、Reverse Proxy 與網路

對外公開時必須由 Caddy、Nginx、Traefik 或 NAS 反向代理終止 TLS。以下 Nginx 範例假設 TLS 憑證已由你的 ACME／憑證管理流程提供：

```nginx
server {
    listen 443 ssl http2;
    server_name inktime.example.net;

    ssl_certificate     /etc/letsencrypt/live/inktime.example.net/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/inktime.example.net/privkey.pem;

    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 10s;
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
    }
}
```

Nginx 負責 TLS 與公開入口限流；InkTime 負責 Session／CSRF／CSP／HSTS（只有確認 HTTPS request 才送出）。只開放 Proxy 的 443，不公開 SQLite 或 `/data`，並限制管理頁來源網段。Webhook 會把通知資料送到外部 HTTPS 服務；Bearer Token、Device Secret 與 pairing code 都不得放在 URL／Log，目的地不得使用 redirect 或內部位址。ESP32 韌體若未配置可信 CA，不應直接跨不可信公網使用 HTTPS，建議先用隔離 IoT VLAN＋反向代理或 VPN。

## 8. 更新、備份與回滾

NAS 若要避免每次 `git pull` 與本機 Build，可改用 `docker-compose.nas.yml`。版本合併到 `main` 後建立 `vX.Y.Z` Git Tag，GitHub Actions 會發布 GHCR 多架構映像；NAS 只需執行 `./scripts/update_nas.sh vX.Y.Z`。首次設定、`latest` 規則、私有 Package 登入與 Schema 回復邊界見 [NAS 以 Git Tag 更新 InkTime Docker](NAS_TAG_DEPLOYMENT_ZH_TW.md)。

保留原始碼並在部署主機 Build 的既有流程如下。

更新前在「備份」建立並下載一份備份，再執行：

```bash
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8765/health/ready
```

只有存在新 Migration 時才建立升級前 SQLite 備份；普通三服務重啟不再各複製一份資料庫。回滾需停止三服務，依[備份還原指南](BACKUP_RESTORE_ZH_TW.md)執行 `scripts/restore_backup.py --yes`；工具會驗證 Schema／SHA-256／integrity／重要表筆數並保留還原前安全副本，不能在線上替換使用中的 SQLite。

## 9. 驗收清單

- [ ] 三服務 `healthy`，沒有 restart loop。
- [ ] `/data` 可寫，`/photos` 唯讀且可讀。
- [ ] Web 診斷顯示 cgroup 記憶體上限與合理 RSS。
- [ ] 待機 10 分鐘後 `docker stats --no-stream` CPU 接近 0%，沒有每 2 秒固定喚醒。
- [ ] 建立 10 張掃描／分析工作，Docker Log 只有彙總進度而非逐張成功紀錄。
- [ ] 重啟 Worker 後工作可從租約恢復。
- [ ] ESP32 取得 Manifest、下載 96,000-byte 檔案、回報韌體與訊號。
- [ ] 備份可下載並通過完整性檢查。

## 10. 純軟體長時間驗證

CI 的 `workflow_dispatch` 可執行 30 分鐘、2 小時或 5 小時 bounded soak，並上傳不含 Token、認證資料或宿主路徑的 JSON summary。24 小時 soak 只在受控 LAN 主機本地執行：

```bash
python scripts/runtime_soak.py --duration-seconds 86400 \
  --timeout-seconds 86520 --summary-json /tmp/inktime-soak-24h.json
```

Soak 會檢查 RSS、FD、thread、SQLite connection／WAL、child process、pending async work、job age 與 Scheduler heartbeat；timeout、未清理資源或超過門檻皆非 PASS。
