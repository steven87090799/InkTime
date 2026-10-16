# NAS 首次使用 Checklist

適用於 UGREEN 原生備份的共用照片庫、Immich 等應用程式唯讀共用，以及接近十萬張照片的長期部署。首頁「完整設定導覽」會依目前模式分成必要、建議與可調整三組；打勾只代表設定條件成立，請完成下方小量驗證後再處理全庫。

## 必須完成的設定

- [ ] **指定正式映像版本**：從 [GitHub Releases](https://github.com/steven87090799/InkTime/releases) 下載該版本的 `inktime-nas-vX.Y.Z.zip`，解壓後將 `.env.example` 複製成 `.env`。安裝包已指定 `INKTIME_IMAGE_TAG=vX.Y.Z`；容器映像為 `ghcr.io/steven87090799/inktime:vX.Y.Z`。等待該版本的映像發布成功才可部署。NAS 只需拉取預建映像，不需 Python、Git 或編譯環境。
- [ ] **照片路徑**：將 `INKTIME_PHOTO_PATH` 指向 UGREEN 正在備份的既有相簿根目錄，不必按年、月建立多個照片庫。InkTime 會遞迴掃描；與 Immich 共用同一路徑即可。保留 Compose 的 `/photos:ro`，不要複製、移動或重新分類原檔。
- [ ] **資料路徑**：`INKTIME_DATA_PATH` 使用 NAS 本機 ext4／xfs／btrfs 上獨立的持久目錄，不能放在照片樹內、不能使用 SMB／NFS 作為 SQLite 儲存。照片與資料路徑均須已存在，且不得互為父子目錄。只讓 UID/GID `10001:10001` 能寫入 InkTime 資料目錄；照片只需讀檔與逐層穿越目錄的權限，不要為本專案遞迴更改共用相簿的擁有者。
- [ ] **網址與連線**：填妥 `INKTIME_PUBLIC_URL`、`INKTIME_BIND_ADDRESS` 與時區 `TZ=Asia/Taipei`。HTTPS 反向代理請保留安全 Cookie；若只在可信任 LAN 用 HTTP，必須一起設定 `INKTIME_COOKIE_SECURE=0`、`INKTIME_ALLOW_INSECURE_HTTP=1`、`INKTIME_PROXY_TRUST=0`，網址與 NAS LAN IP 要一致。詳細範例見 [NAS 部署指南](../operations/NAS_TAG_DEPLOYMENT_ZH_TW.md)。
- [ ] **啟動與管理員**：UGREEN Docker Project 匯入安裝包的 `docker-compose.nas.yml` 和 `.env`，或使用下列命令。確認 web、worker、scheduler 都健康，再從 `/setup` 建立第一位管理員；不要重新初始化既有資料庫。

```bash
docker compose --env-file .env -f docker-compose.nas.yml pull
docker compose --env-file .env -f docker-compose.nas.yml up -d --no-build
docker compose --env-file .env -f docker-compose.nas.yml ps
```

- [ ] **先掃描，再複核**：維持 `analysis.execution_mode=local_only`，在「維護」掃描容器內 `/photos`，在照片庫確認照片數、年／月分布、方向、拍攝時間、重複檔與掃描失敗。先解決權限／解碼錯誤，不能把 `completed_with_warnings` 當成全庫完整驗收。拍攝日期取照片資訊，資料夾名稱本身不保證是拍攝年月。
- [ ] **送出前排除**：保留 `analysis.prefilter_enabled`、`analysis.prefilter_screenshots`、`analysis.prefilter_low_quality`，先複核截圖、低品質與人工排除清單。所有已排除／禁止上傳的照片都不能以強制分析繞過；要送出必須先明確恢復。自動判斷有誤判與漏判的可能，無法宣稱任意照片百分之百分類正確。
- [ ] **需要模型時才設定**：在「模型與 API」啟用支援 Vision 的 Provider，填妥 Base URL、模型 ID、Key、價格。在「設定」使用 `analysis.execution_mode=automatic_ai`、`analysis.ai_mode=full_library`、`analysis.strategy=single`，讓每張合格照片建立描述；純本機模式不需要 Key。設定每日／每月張數與預算停用上限，先用少量非敏感照片核對實際費用及保存結果。連線或圖片測試也可能計費。
- [ ] **先建立可還原備份**：建立一次應用程式資料庫備份，另將完整 `/data` 與原始照片安排獨立的異機備份。保留 `/data/session.key`、資料庫、分析輸入輸出檔與部署 `.env`；Key 與備份請限制讀取權限。網頁 ZIP 主要保存資料庫，不等於整個 `/data` 備份，也不包含原始照片。複製 live SQLite 主檔時不可漏掉 WAL；請使用應用程式的一致性備份，或停止三個服務後再完整備份資料目錄。
- [ ] **相框與小量驗收**：核對裝置面板、橫／直方向、時區及版型。雙照片拼版、雙照片各自一句話與智慧自適應的雙圖畫面，都會在每張照片顯示文句與較小日期；缺日期留空。分別驗證橫向兩直圖、直向兩橫圖，先預覽、再產生新發布並檢查實際相框。已發布檔案不會因更新程式而原地重畫。

## 非必要但建議查看的設定

- [ ] **掃描範圍與新備份**：確認 NAS ACL 對每層子目錄均可讀；未完成複製的照片待備份完成後再掃描。隱藏的使用者相簿也會掃描；不跟隨 symlink，並略過已知 NAS 回收／縮圖／快照系統目錄及 AppleDouble sidecar。檢查略過／不支援／解碼失敗檔案，不能僅以 NAS 總檔案數當成受支援照片數。目前支援 JPEG、PNG、WebP、HEIC／HEIF、TIFF、BMP；單檔上限 200 MiB、6000 萬像素、最長邊 12000px。不支援的常見 RAW／AVIF／JXL 等會列為掃描錯誤；影片與 GIF 另計排除。避免重疊登錄父、子相簿。
- [ ] **排程**：啟用增量掃描、分析、渲染及備份，避免與 UGREEN 備份、Immich 大量匯入同時競爭磁碟。新增照片會在後續掃描納入，已完成且內容相同的分析應沿用。
- [ ] **低並行起步**：Intel N100 先用 `analysis.concurrency=1`、`worker.queue_multiplier=1`，查看 RSS、處理速度、磁碟空間與 Provider 限流後再調高。十萬張驗收須包括完整遍歷、錯誤計數、恢復後的配對及實際費用，CI 不等於已在你的照片庫跑完。
- [ ] **完整紀錄與容量**：保留模型輸入、回應、照片 SHA、模型與提示詞版本。精確送出影像在 `/data/model-inputs`，完整請求／回應及內容版本在資料庫；`batch.local_retention_days=0` 預設永久保存 Batch JSONL。分析追蹤與 Batch 檔案含敏感資訊且會增加容量，勿只備份照片而忽略 `/data`。操作監控的有界追蹤畫面不是完整資料的替代品。
- [ ] **錯誤處理**：逾時／斷線後若無法確認 Provider 是否已接受請求，先查供應商紀錄與本機 checkpoint；不要刪工作、清資料庫或一再重送。沒有 Provider 的冪等／結果查詢保證時，無法同時承諾「絕不重送」與「所有不明請求必定自動完成」，系統會優先避免重複付費。
- [ ] **備份演練**：在另一個資料目錄或測試主機還原，核對照片數、分析數、SHA 與配對，再允許任何模型請求。`session.key` 損失會影響加密設定；單一 NAS 磁碟內的備份不能防止整機遺失。
- [ ] **離線相框**：查看裝置最後錯誤、設定 ACK、排程版本與下載結果。服務健康不代表實板已套用排程，重發圖片也不會修好韌體通訊問題。

## 可依照需求調整的設定

| 設定 | 用途與建議 |
| --- | --- |
| `render.frame_orientation`、裝置個別方向 | 按相框實際擺放方向設定；裝置個別值優先。 |
| `render.layout`、裝置個別版型 | 選單張資訊、雙圖文字或智慧自適應；雙圖兩種方向都支援文句與日期。 |
| `render.show_capture_date` | 顯示／隱藏小日期；無有效拍攝日期時始終不顯示未知日期佔位。 |
| 字型、縮放、文句與照片來源 | 先看預覽，再發布新圖片；修改渲染不需要重新送模型。 |
| `analysis.prefilter_sensitivity` | 先用保守模式；調整前複核夜景、老照片與截圖邊界。 |
| 每日／每月張數、預算、排程時段 | 分階段處理全庫，不要用無限制重試追求進度。 |
| 備份時間與 `backup.retention` | 避開大量分析時段，依容量與恢復需求保留多份及異機副本。 |
| 固定版本或 `latest` | 固定 `vX.Y.Z` 便於驗證與回復；明確選 `latest` 才跟隨後續穩定版。 |

## 更新、重裝與搬移

1. 更新前建立一致性備份，記下目前映像 tag／digest、資料與照片實際路徑。
2. 更換映像版本，沿用同一個 `INKTIME_DATA_PATH`、`INKTIME_PHOTO_PATH` 與 `session.key`；執行上述 pull／up，不使用 `down -v` 或清空資料目錄。
3. 換 NAS 時先還原完整資料目錄、原始照片與 `.env`，讓容器內路徑仍為 `/data`、`/photos`；先以本機模式核對，再開啟模型。
4. 同內容照片搬移／重複檔應按內容雜湊沿用結果；同一路徑換成不同內容是新的照片版本，舊結果需保留於歷史紀錄，不能套用到新內容。
5. Schema 升級後不要直接用舊程式讀取新資料庫。需回復舊版時，停止所有服務並使用相容的一致性快照；完整復原流程見 [NAS 指南](../operations/NAS_TAG_DEPLOYMENT_ZH_TW.md)及 [正式部署指南](../operations/PRODUCTION_DEPLOYMENT_GUIDE_ZH_TW.md)。
