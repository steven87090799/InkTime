# InkTime 安裝指南

## 支援環境

- Docker Engine 24+／Compose v2（建議）；或 Python 3.10+、Linux／macOS。
- SQLite 資料目錄需可寫；照片目錄建議唯讀掛載。
- Intel N100 4C/4T 可正式部署；主機至少 4 GiB RAM，建議 8 GiB。N100 預設 Worker 並行 1、Web 1 worker × 2 threads。

## Docker

```bash
# 可信任 LAN／本機 HTTP：
cp .env.local.example .env
# 正式 HTTPS Reverse Proxy 則改用：
# cp .env.production.example .env
# 設定實際 INKTIME_PUBLIC_URL、INKTIME_PHOTO_PATH 與 INKTIME_DATA_PATH
mkdir -p data
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8765/health/ready
```

若容器 UID 10001 無法寫入資料目錄，執行 `sudo chown -R 10001:10001 <data-path>`。照片目錄只需讀取權限。

## 原生安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/migrate.py --database data/inktime.db
gunicorn --config gunicorn.conf.py server:app
```

另開程序執行 `python -m inktime.app.workers.runner` 與 `python -m inktime.app.workers.scheduler`。只有本機開發可執行 `python server.py`。

## 首次啟動

瀏覽 `/setup` 建立 administrator。新帳號需 3–64 個 ASCII 識別字元，密碼需 12–128 字元且不會裁切前後空白。LAN HTTP 使用 `COOKIE_SECURE=0`／`ALLOW_INSECURE_HTTP=1`；Production 預設且建議 HTTPS、`COOKIE_SECURE=1`／`ALLOW_INSECURE_HTTP=0`。Production 若明確改用 insecure HTTP break-glass，Health／Preflight 會 degraded，且不可公開至公網。反向代理只可傳入可信任的 Host、Proto 與來源 IP 標頭。安裝後立即建立備份並測試下載。
