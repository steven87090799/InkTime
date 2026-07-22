# InkTime Docker 部署規格（Intel N100）

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

## 3. 首次部署

```bash
git clone <你的 InkTime 私有儲存庫 URL> InkTime
cd InkTime
cp .env.example .env
mkdir -p data
sudo chown -R 10001:10001 data
```

編輯 `.env`，至少確認：

```dotenv
TZ=Asia/Taipei
INKTIME_PORT=8765
INKTIME_DATA_PATH=/srv/inktime/data
INKTIME_PHOTO_PATH=/mnt/photos
INKTIME_COOKIE_SECURE=0
INKTIME_IMAGE_TAG=local
```

不要把 `.env`、資料庫、`session.key`、API Key 或裝置 Token Commit。啟動：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8765/health/ready
```

三個服務都應顯示 `healthy`。瀏覽 `http://N100-IP:8765/` 建立非空白的管理員密碼；系統不限制長度，但正式環境仍建議使用密碼管理器產生的長密碼。

## 4. `.env` 部署欄位

| 欄位 | 預設 | 說明 |
|---|---|---|
| `INKTIME_PORT` | `8765` | 主機對外 Port |
| `INKTIME_DATA_PATH` | `./data` | SQLite、快取、字型、備份、發布；可寫 |
| `INKTIME_PHOTO_PATH` | `./simulation_photos` | 原始照片；容器內固定 `/photos` 且唯讀。無實體面板時可直接使用專案內投放區 |
| `INKTIME_COOKIE_SECURE` | `0` | HTTPS 反向代理完成後設 `1` |
| `INKTIME_ACCESS_LOG` | `0` | 是否逐一輸出 HTTP request；正式環境維持關閉 |
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
6. 「裝置」：建立 ESP32、設定時區／每日刷新／旋轉並複製一次性 Token。
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

## 7. HTTPS 與網路

對外公開時必須由 Caddy、Nginx、Traefik 或 NAS 反向代理終止 TLS，並把 `.env` 的 `INKTIME_COOKIE_SECURE=1` 後重建容器。只開放 Web Port，不公開 SQLite 或 `/data`；限制管理頁來源網段。ESP32 韌體若未配置可信 CA，不應直接跨不可信公網使用 HTTPS，建議先用隔離 IoT VLAN＋反向代理或 VPN。

## 8. 更新、備份與回滾

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
