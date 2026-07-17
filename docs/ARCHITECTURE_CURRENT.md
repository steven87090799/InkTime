# 目前架構

```text
config.py
   ├── analyze_photos.py ── HTTP Vision API
   │       ├── 相片檔案／EXIF
   │       └── photos.db: photo_scores
   ├── render_daily_photo.py ── output/*.bin
   ├── render_daily_photo_133c.py ── output/*.bin
   └── server.py
           ├── 內嵌 HTML / review / simulator
           ├── 直接讀取 photos.db
           ├── 直接執行圖片渲染
           └── 共用 URL 金鑰下載

ESP32 ── GET /static/inktime/<key>/... ── server.py
```

## 邊界與耦合

- `config.py` 在 import 時決定全域路徑、模型與金鑰。
- `server.py` 同時負責路由、SQL、HTML、檔案安全與渲染。
- `analyze_photos.py` 同時負責掃描、NAS 重連、影像編碼、Provider 切換、提示詞、解析、EXIF 與持久化。
- 兩個渲染器在模組載入時固定 `TODAY`，日期與字型策略分散。
- 程序之間只透過固定檔案與 SQLite 隱式協作，沒有工作租約、事件或健康狀態。

## ESP32 通訊

韌體儲存伺服器位置與下載路徑；排程醒來後下載固定檔案。伺服器只比對 URL 中的共用字串，未識別裝置、未回傳 Manifest，也未記錄校驗與下載結果。下載失敗處理依各韌體分支而異。

## 執行與部署

README 建議直接執行 Flask Development Server 與 cron。沒有正式 WSGI 設定、獨立 Worker、Scheduler、Healthcheck、非 root 映像檔、結構化 Log 或優雅關閉契約。
