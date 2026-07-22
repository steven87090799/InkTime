# 管理員指南

## 角色

- administrator：設定、工作控制、Provider、裝置、發布、備份與錯誤處理。
- viewer：只讀照片、成本、工作、診斷與匯出。

## 設定欄位

| 欄位 | 預設 | 合法範圍／建議 | 風險 | 重啟 |
|---|---:|---|---|---|
| `general.timezone` | Asia/Taipei | IANA 時區 | 影響跨日與排程 | 否 |
| `analysis.strategy` | smart_two_stage | 五種策略 | 高品質成本高 | 否 |
| `analysis.stage_two_threshold` | 65 | 0–100，建議 60–75 | 越低成本越高 | 否 |
| 本機預篩選 | 啟用 | 截圖／明顯低品質可分別停用 | 排除項目 0 Token；不刪原檔 | 否 |
| `analysis.prefilter_sensitivity` | conservative | conservative／balanced／aggressive | 越積極越省 Token，也越可能誤排除 | 否 |
| `analysis.e6_prefilter_enabled` | true | true／false | 關閉後不會因六色量化失真而省下模型請求 | 否 |
| `analysis.e6_min_score` | 25 | 0–100，建議 20–35 | 越高越省 Token，但可能排除原圖好看、六色表現較弱的照片 | 否 |
| `analysis.scoring_rules` | 內建完整規則 | 100–12000 字元 | 影響新分析結果 | 否 |
| 綜合排序權重 | 50／20／10／20 | 四項合計 100% | 影響新分析與自動選片順序 | 否 |
| 最愛照片加分 | 5 | 0–30 | 只加入綜合排序分 | 否 |
| `analysis.concurrency` | 1 | 1–8，Intel N100 建議 1；確認 RSS 後最多先試 2 | 過高觸發限流／圖片記憶體尖峰 | 否 |
| `worker.queue_multiplier` | 1 | 1–4，N100 建議 1 | 增加記憶體中 Future | 否 |
| `worker.poll_seconds` | 15 | 1–300；低待機可設 30–60 | 越小待機喚醒越多 | 否 |
| `worker.progress_items` | 50 | 5–10,000 | 越小 Docker Log 越多 | 否 |
| `worker.progress_seconds` | 300 | 30–3,600 | 越小 Docker Log 越多 | 否 |
| `scheduler.poll_seconds` | 60 | 30–3,600 | 越小 SQLite／CPU 喚醒越多 | 否 |
| `analysis.max_retries` | 3 | 0–10 | 重試增加成本 | 否 |
| `model.low_model` | gpt-4o-mini | 支援圖片／Schema 的模型 | 能力不足會進錯誤佇列 | 否 |
| `model.high_model` | gpt-4o | 同上 | 先設定價格 | 否 |
| `budget.daily_warning` | 5 | ≥0 美元 | 只警告 | 否 |
| `budget.daily_stop` | 10 | ≥0 美元 | 達到即停新請求 | 否 |
| `budget.monthly_warning` | 50 | ≥0 美元 | 只警告 | 否 |
| `budget.monthly_stop` | 100 | ≥0 美元 | 達到即停新請求 | 否 |
| `budget.job_default` | 10 | ≥0 美元 | 工作達到後暫停 | 否 |
| `budget.photo_max` | 0.25 | ≥0 美元 | 過低阻擋第二階段 | 否 |
| `budget.max_tokens` | 8000 | 256–1,000,000 | 需符合模型能力 | 否 |
| `render.memory_threshold` | 70 | 0–100 | 過高可能無候選 | 否 |
| `render.quantity` | 5 | 1–50 | 增加下載量 | 否 |
| `render.selection_mode` | history_today | history_today／top_ranked | 歷年今日會依系統時區與 EXIF 拍攝日選片 | 否 |
| `render.history_today_window_days` | 7 | 0–31 | 0 只接受完全相同月日 | 否 |
| `render.history_today_fallback` | nearby_then_ranked | nearby_then_ranked／nearby_only／ranked／none | 限制越嚴格越可能沒有足量候選 | 否 |
| `render.e6_weight` | 20 | 0–60% | 過高會讓面板顯示效果凌駕回憶分 | 否 |
| `render.layout` | photo_info | full／postcard／photo_info／calendar／weather_sensor | 日曆與天氣版型的照片區較小 | 否 |
| `render.show_capture_date` | true | true／false | EXIF 日期錯誤時也會跟著顯示 | 否 |
| `render.font_path` | 內建芫荽 | 內建手寫／文青風格或已上傳 TTF／OTF／TTC | 缺字會停止發布，不會 fallback | 否 |
| `render.show_location` | true | true／false | 只顯示最近城市，不顯示座標 | 否 |
| `render.location_max_distance_km` | 80 | 1–500 公里 | 過大可能顯示不準確的鄰近城市 | 否 |
| `render.profile` | safe_4c | 四色／GDEP 六色／GDEY 七色 | 必須與裝置面板相符 | 否 |
| `render.dither` | floyd_steinberg | 原廠相容／照片平滑／Floyd／Atkinson／Bayer／none | 照片平滑可能柔化極細線；兩種新模式強度固定 | 否 |
| `render.dither_strength` | 1 | 0–2 | 過高會增加色點 | 否 |
| `render.color_distance` | oklab | oklab／rgb | 切換會改變色彩映射 | 否 |
| `render.weather_enabled` | false | 啟用前先填正確經緯度 | 需連外；失敗不阻擋照片發布 | 否 |
| 天氣經緯度／顯示名稱 | 臺北市中心／所在地 | 緯度 -90–90、經度 -180–180 | 預設座標只是範例，啟用前必須修改 | 否 |
| `render.sensor_device_id` | 空白 | PhotoPainter 裝置 ID；空白取最近回報 | 多裝置時可能抓到別的房間 | 否 |
| `device.legacy_api_enabled` | false | 僅遷移期 | URL 金鑰不安全 | 是 |
| `device.default_timezone` | Asia/Taipei | IANA 時區 | 影響新增裝置排程 | 否 |
| `device.default_schedule` | 08:00 | 00:00–23:59 | 影響新增裝置刷新時間 | 否 |
| `device.default_rotation` | 0 | 0／180 | 目前 7.3 吋正式韌體限制 | 否 |
| `device.default_panel_profile` | safe_4c | 四色／GDEP 六色／GDEY 七色 | 型號錯誤會由韌體拒絕 | 否 |
| 離線／恢復通知 | 30 小時／啟用 | 1–720 小時；掃描預設 300 秒 | 需大於裝置刷新週期 | 否 |
| 離線重複提醒 | 停用／冷卻 24 小時 | 1–720 小時 | 過短會造成通知轟炸 | 否 |
| Webhook | 停用 | 完整 HTTP(S) URL、2–30 秒逾時 | 只連可信端點；Token 加密保存 | 否 |
| `system.log_level` | INFO | DEBUG／INFO／WARNING／ERROR／CRITICAL | DEBUG 增加磁碟寫入 | 否 |
| `system.log_format` | json | human/json | 集中 Log 建議 json | 否 |
| `system.diagnostics_cache_seconds` | 300 | 30–86,400 | 太小會反覆掃大型縮圖目錄 | 否 |
| `security.session_minutes` | 30 | 5–1440 | 過長增加共用裝置風險 | 否 |
| `backup.schedule_enabled` | true | true/false | 關閉後需手動備份 | 否 |
| `backup.hour` | 3 | 0–23 | 避開大量分析 | 否 |
| `backup.retention` | 14 | 1–365 | 過低縮短回復期 | 否 |

所有修改寫入 `setting_history`，最近 100 筆直接顯示在設定頁；Secret 永不寫入摘要。Web、Worker、排程、Log 與 Session 的新設定均動態生效。只有舊版裝置 API 這類啟動時安全邊界仍需重啟。

## Web 與部署設定的邊界

不需要修改 Python。分析、排程、模型、成本、渲染、裝置、Log 層級、Session 與備份都由 Web 控制。宿主機 Volume、Port、映像 Tag、HTTPS Secure Cookie、Docker CPU／RAM／PID 上限與 logging driver 必須在容器啟動前由 `.env`／Compose 決定；容器內程式不應取得 Docker socket 去改寫宿主機。設定頁會只讀顯示目前部署資訊。

## 繁體中文字型

「渲染」頁離線內建兩套 SIL OFL 1.1 字型，不需要主機預先安裝，也不需要在執行時連外下載：

- 芫荽 Iansui v1.020：手寫風格，採臺灣教育部標準字形取向，預設啟用。
- 霞鶩文楷 TC v1.522：文青風格，帶楷體筆意與書卷感。

頁面顯示由伺服器實際載入 TTF 後產生的預覽圖，不是瀏覽器近似 fallback。管理員可一鍵切換；viewer 只能查看。自訂上傳支援 TTF／OTF／TTC、上限 64 MiB，會先解析檔案並檢查基本繁中字元，再以原子替換寫入 `/data/fonts`，失敗不會覆寫同名可用字型。

這項安裝檢查不取代正式渲染檢查。每段短文案仍會逐字比對目前字型的 cmap；缺少任一非空白字元就回報 `IMG-002` 並停止該次發布，不會載入 Pillow 預設字型。兩套內建字型的來源、固定 SHA-256 與授權全文位於 `inktime/app/domain/rendering/font_assets/`。

照片含 GPS 時，正式渲染預設會在短文案下方加入「地點｜最近城市」。城市由 `data/world_cities_zh.csv` 離線比對，精確經緯度不會印在畫面；超過 `render.location_max_distance_km` 找不到可信城市時就不顯示。可用 `render.show_location` 完全停用。

## 智慧裁切、E6 適合度與相框版型

「渲染」頁提供即時六色預覽。智慧裁切先用本機 OpenCV 尋找正面人臉；沒有可信人臉時，改以邊緣、色彩與中央先驗估計主體。裁切會盡量保留偵測到的主體範圍，管理員也可用水平／垂直滑桿覆寫焦點並儲存，或恢復自動模式。這些操作只儲存 0–1 的相對位置，不修改原始照片。

E6 適合度會在任何模型請求前，以正式 `gdep073e01_6c` 色盤、OKLab 色差與 Bayer 抖動建立 112 px 本機樣本，量測量化後對比保留、主體細節、膚色偏差與強邊緣／文字可讀性。總分低於 `analysis.e6_min_score` 時可直接排除，因此不新增 Token；最愛照片仍會略過排除。舊照片第一次進入候選或渲染時會自動補算，仍不呼叫模型。

五種版型為全版照片、明信片、照片＋日期地點、月曆相框、天氣＋室內溫溼度。預覽可暫時切換版型，按「設為預設版型」才會改正式發布設定。天氣資料為選用功能，從 Open-Meteo 取得目前天氣、溼度與當日高低溫並快取 30 分鐘；外部服務失敗時照片仍正常發布。室內資料來自 PhotoPainter 裝置狀態回報；沒有感測值時畫面會明確顯示尚無回報。

## 歷年今日選片

預設 `history_today` 不是單純挑最高分：依 `general.timezone` 的今天，先找「月、日相同且年份早於今年」的照片；不足時在預設前後 7 日內依日期距離補足，再依綜合排序回退。可把回退改成只接受鄰近日、直接採排名或完全不補圖，也可切換 `top_ranked`。手動指定照片發布時永遠採管理員選擇，不受自動選片規則限制。

## 本機預篩選與 ExifTool 邊界

照片掃描只建立 JPG、PNG、WebP、HEIC／HEIF、TIFF、BMP 等靜態照片；MOV、MP4、M4V、MKV、WebM 與 GIF 動畫會計入 `excluded_videos` 後停止，不會建立模型工作。雲端分析前再依本機檔名、尺寸、格式、相機 EXIF、模糊、對比、曝光與解析度判斷：截圖達門檻即可排除；一般照片必須同時出現至少兩項明顯缺陷才排除。人工標記為最愛的照片永遠略過此預篩選。

ExifTool 能提供 MIME、相機、軟體、拍攝時間與 GPS 等中繼資料，但不能可靠判斷構圖、人物表情或「好不好看」。目前正式流程直接用 Pillow 讀取 EXIF／GPS，不要求容器安裝 ExifTool；畫質則以本機縮圖特徵判斷。這可避免每張照片額外啟動外部程序，也不會把照片或座標傳到第三方服務。

## ESP32 遠端設定

首次 AP 配對只填 Wi-Fi、InkTime URL 與一次性 Token。之後從「裝置」編輯每台 ESP32 的名稱、啟停、面板 Profile、IANA 時區、每日 `HH:MM` 與 0°／180°；下一次取得 Manifest 自動套用。裝置頁以期望版本／ACK 區分「已儲存」與「裝置已生效」，並顯示離線狀態、通知、firmware、RSSI、free heap／PSRAM、下載計數與最後錯誤。完整協定、抖動與通知見[裝置可靠性與六／七色渲染指南](DEVICE_COLOR_NOTIFICATION_GUIDE_ZH_TW.md)。

## 裝置能源儀表板

「能源」頁可依裝置與 7／30／90／365 天期間查看電池百分比、電壓、刷新耗時、最近
樣本與續航估算。平台保留最近 400 天低頻遙測；USB 供電樣本不會被當成電池放電。

續航分成「實際放電趨勢」與「容量／電流模型」。後者需由管理員填入電池容量、整板
deep-sleep 待機電流、完整喚醒週期平均電流、每日刷新次數及安全保留百分比。電流必須
由外接功率計量測；未填入時頁面顯示「待量測」，不會套用晶片 datasheet 猜測值。
每次變更會寫入裝置事件；viewer 可以查看曲線與假設，但不能修改模型參數。

## 照片評分與門檻

模型會直接輸出回憶、美觀、技術品質與情緒四個 0–100 原始分數。系統另用「評分」頁的四項權重算出 `ranking_score`，並在最愛照片上加入設定的額外分數；原始四項分數不會被覆寫。`analysis.stage_two_threshold` 仍只決定是否進入第二階段，`render.memory_threshold` 仍是電子紙候選的最低回憶分門檻。

- 改模型：在「設定」調整 `model.low_model`／`model.high_model`，並在「模型」頁設定 Provider。
- 改第二階段成本與品質取捨：調整 `analysis.stage_two_threshold`。
- 改電子紙最低回憶分：調整 `render.memory_threshold`。
- 改模型評分規則或綜合權重：到「評分」頁儲存為新版本；下一次分析立即生效，既有照片不會自動重算。
- 測試照片：在「評分」頁選一張照片並確認付費請求；暫存檔會在請求結束後刪除，Token、費用與延遲仍寫入成本紀錄。
- 還原：版本歷史的「還原此版本」會建立一個新的目前版本，不會刪除或覆寫任何歷史。
- 預設值已整理自舊版 `legacy_analyze_photos.py`，新版版本化預設位於 `inktime/app/domain/analysis/scoring.py`。
- JSON Schema、繁體中文與不得虛構等固定約束不允許從網頁覆寫，位於 `inktime/app/providers/openai_compatible.py`。

完整流程圖與程式入口見 [專案架構與評分流程](ARCHITECTURE_ZH_TW.md)。

## 排程換圖與不合格照片

`display_prepare` 支援且只支援 `display_times`、`lead_minutes`、`daily_count`、`device_ids`、`candidate_years`、`prefetch_count`、`ai_fallback`、`render_fallback`。未知欄位不會被靜默忽略。`device_ids` 解析為實際啟用裝置的 Profile；`daily_count × prefetch_count` 決定候選數量；年份會直接限制 SQL 候選。

人工排除、自動排除、Missing、deleted、路徑逃逸、原始檔缺失或沒有最新分析的照片均不能正式發布。管理員明確指定這類照片會收到 `RENDER-009`，系統不會換成另一張照片。
