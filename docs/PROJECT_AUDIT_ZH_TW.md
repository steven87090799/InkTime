# InkTime 工程現況稽核

稽核基線：`main` / `b09baab`，2026-07-17。本文描述重構開始前的實際狀態，不代表目標平台已完成。

## 現有組成

| 元件 | 入口 | 現況 |
|---|---|---|
| 照片分析 | `analyze_photos.py` | 1,414 行單檔；掃描、EXIF、模型、重試與 SQLite 寫入混合 |
| Web 與下載 | `server.py` | 2,073 行單檔；HTML 內嵌；無登入；直接查詢 SQLite |
| 四色渲染 | `render_daily_photo.py` | 480 × 800；輸出 1 byte/px |
| 13.3 吋渲染 | `render_daily_photo_133c.py` | 支援分片與 4bpp 格式 |
| ESP32 | `esp32/` | 以 URL 內共用下載金鑰拉取固定檔名 |
| 設定 | `config.py` | 需由使用者複製範例並直接修改 Python |

## 現有資料流程

1. 分析腳本遞迴掃描 `IMAGE_DIR`，用絕對路徑比對 `photo_scores.path`。
2. 每張未處理照片先呼叫模型產生描述、類型與分數，再上傳同一張照片呼叫模型產生短文案。
3. EXIF、模型原始 JSON、分數與絕對路徑寫入單一 `photo_scores` 表。
4. 渲染腳本載入候選照片，以「歷史今日」規則選片，輸出固定名稱 `.bin`。
5. ESP32 透過含共用金鑰的 URL 下載檔案並顯示。

## 現有資料庫 Schema

`photo_scores.path` 是主鍵；另有 `caption`、`type`、`memory_score`、`beauty_score`、`reason`、尺寸、方向、`used_at`、EXIF 展開欄位、`side_caption`、`raw_json`。Schema 由應用程式啟動時反覆執行 `ALTER TABLE`，捕捉所有 `sqlite3.OperationalError` 後忽略，無版本、交易、備份或失敗停止機制。

## 現有 HTTP API

- `/review`：照片列表。
- `/sim`、`/sim_render`：電子紙模擬與即時渲染。
- `/images/<path>`：原始照片傳送。
- `/api/md_list`：可用月日清單。
- `/files/<path>`：輸出目錄瀏覽與下載。
- `/static/inktime/<key>/photo_<n>.bin`、`latest.bin`、`preview.png`：ESP32 舊版下載。

以上管理與照片介面均無身分驗證、角色授權或 CSRF 保護。

## 安全問題

- `_safe_join` 以字串 `startswith()` 判斷路徑，會誤接受相似前綴，且無統一 URL 解碼與跨平台拒絕規則。
- Web 預設監聽 `0.0.0.0` 且無登入；管理介面可洩漏照片、路徑、GPS 與模型內容。
- ESP32 共用金鑰在 URL、韌體與啟動 Console 出現，容易被 Proxy、瀏覽紀錄與 Log 收集。
- API Key 由 Python 設定檔載入；Debug 模式可能印出請求本文。
- `/files` 暴露整個輸出目錄，且沒有角色限制。
- 無穩定錯誤碼、稽核軌跡、登入限制或敏感資料遮罩。

## 效能與擴充性問題

- `list_images()` 與後續 Future 提交會將全部路徑與工作物件留在記憶體；不適合 100,000 張照片。
- `load_sim_rows()`、月日快取建立與部分頁面仍會全表載入。
- 照片以可變的絕對路徑識別；移動檔案會再次分析。
- 沒有 SHA-256、感知雜湊、縮圖快取、重複群組或本地品質初篩。
- HTTP Request 與腳本程序沒有可恢復 Job；無逐張狀態、暫停、取消、預算或重啟續跑。
- SQLite 未一致啟用 WAL、外鍵、busy timeout 與索引檢查。

## Token 成本來源

- 每張照片上傳兩次：主要分析一次、`generate_side_caption()` 再一次。
- 預設長邊 2560px，未依分析階段使用 512/1024/1600px 快取。
- 無內容雜湊與近似去重，相同內容可因改名或移動而重複送出。
- 無低成本第一階段、Batch API、使用量記錄、價格模型或預算停止線。
- JSON 不符合格式時會切換 Provider，可能形成不必要的付費重試。

## 相容性與風險

- `photo_scores`、舊 CLI、現有渲染器與舊韌體必須在遷移期可繼續使用。
- 舊 API 預設關閉後，未升級韌體的裝置會停止更新；需由管理員明確開啟舊版模式或更新韌體。
- 正規化資料表採 UUID／內容雜湊；舊資料需要漸進匯入，不能刪除原表。
- 2bpp 格式需韌體與 Manifest 同步協商，不可直接覆蓋舊 1 byte/px 檔案。

## 修改邊界

本次新增模組化 `inktime/` 平台，保留四個舊入口作相容層。重型工作移至 Worker；Route 只做驗證與呼叫 Service；Repository 集中 SQL；Provider 集中外部模型；資料庫設定取代一般 Python 設定。敏感環境變數只保留初始密鑰與部署層資訊。
