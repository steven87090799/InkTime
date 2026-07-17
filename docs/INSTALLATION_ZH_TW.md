# InkTime 安裝指南

## 支援環境

- Docker Engine 24+／Compose v2（建議）；或 Python 3.10+、Linux／macOS。
- SQLite 資料目錄需可寫；照片目錄建議唯讀掛載。
- 正式環境建議至少 2 CPU、2 GiB 記憶體；大量圖片預處理建議 4 CPU、4 GiB。

## Docker

```bash
cp .env.example .env
# 設定 INKTIME_PHOTO_PATH 與 INKTIME_DATA_PATH
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
gunicorn --bind 0.0.0.0:8765 --workers 2 --threads 4 server:app
```

另開程序執行 `python -m inktime.app.workers.runner` 與 `python -m inktime.app.workers.scheduler`。只有本機開發可執行 `python server.py`。

## 首次啟動

瀏覽 `/setup` 建立 administrator。密碼至少 12 字元。反向代理終止 TLS 時設定 `INKTIME_COOKIE_SECURE=1`，並限制 Proxy 傳入可信任的來源 IP 標頭。安裝後立即建立備份並測試下載。
