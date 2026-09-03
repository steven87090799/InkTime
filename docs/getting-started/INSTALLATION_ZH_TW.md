# InkTime 安裝指南

## 支援環境

- Docker Engine 24+／Compose v2（建議）；或 Python 3.10+、Linux／macOS。
- SQLite 資料目錄需可寫；照片目錄建議唯讀掛載。
- Intel N100 4C/4T 可正式部署；主機至少 4 GiB RAM，建議 8 GiB。N100 預設 Worker 並行 1、Web 1 worker × 2 threads。

## Docker

正式 NAS 使用 [NAS Tag 部署指南](../operations/NAS_TAG_DEPLOYMENT_ZH_TW.md)：準備同版 `docker-compose.nas.yml`、更新器與部署契約檔，複製 `.env.nas.example` 為 `.env.nas`，設定已存在的 canonical 資料／照片目錄、實際 URL 與 UID/GID `10001:10001` 權限，再執行：

```bash
sudo ./scripts/update_nas.sh --initialize vX.Y.Z
```

`vX.Y.Z` 是佔位，必須換成已發布版本。日後更新不加 `--initialize`；不要直接 Build 或手動 up 繞過 recovery point。

本機開發／模擬才執行：

```bash
cp .env.local.example .env
# 調整實際 data/photos 路徑、INKTIME_DEV_PUBLIC_URL 與綁定位址。
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.dev.yml ps
```

開發 override 預設對 LAN 開放；僅本機使用時設定 `INKTIME_DEV_BIND_ADDRESS=127.0.0.1`。容器 UID 10001 必須可寫 data；照片只需讀取權限，不要對相簿遞迴 chown。

## 原生安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 三個程序均須使用同一組 RuntimeConfig 環境變數；啟動會自動套用 Migration。
export INKTIME_ENVIRONMENT=development
export INKTIME_DATA_DIR="$PWD/data"
export INKTIME_PHOTO_DIR="$PWD/simulation_photos"
gunicorn --config gunicorn.conf.py server:app
```

原生 Python 不會自動讀取 Compose 的 `.env`。另開程序並重設相同環境後執行 `python -m inktime.app.workers.runner` 與 `python -m inktime.app.workers.scheduler`。只有本機開發可執行 `python server.py`。

## 首次啟動

瀏覽 `/setup` 建立 administrator。新帳號需 3–64 個 ASCII 識別字元，密碼需 12–128 字元且不會裁切前後空白。LAN Production 保持 environment=production，使用 `COOKIE_SECURE=0`／`ALLOW_INSECURE_HTTP=1`，Health／Preflight 明確顯示 trusted-lan-http／degraded；不可公開至公網。HTTPS Production 使用 `COOKIE_SECURE=1`／`ALLOW_INSECURE_HTTP=0`。反向代理只可傳入可信任的 Host、Proto 與來源 IP 標頭。安裝後立即建立備份並測試下載。
