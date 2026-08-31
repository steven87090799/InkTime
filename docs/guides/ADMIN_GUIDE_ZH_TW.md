# 管理員指南

設定表以新安裝預設為準，升級會保留管理員已存的設定。登入 administrator 後在 `/settings` 搜尋中文標籤或 key；本主線仍有「基本／進階」模式，找不到欄位時切到進階並清除篩選。一般 AI 工作必須先把「分析執行模式」設為 `automatic_ai`；只填 Key 或模型不會啟用。追查沒有文案或 Worker 告警見[Activity／AI Trace](ACTIVITY_AI_TRACE_ZH_TW.md)。


## 角色

- administrator：設定、工作控制、Provider、裝置、發布、備份與錯誤處理。
- viewer：只讀照片、成本、工作、診斷與匯出。

## 設定欄位

| 欄位 | 預設 | 合法範圍／建議 | 風險 | 重啟 |
|---|---:|---|---|---|
| `general.timezone` | Asia/Taipei | IANA 時區 | 影響跨日與排程 | 否 |
| `analysis.execution_mode` | local_only | disabled／local_only／local_with_manual_ai／automatic_ai | 只有明確開啟 AI 才呼叫 Provider | 否 |
| `analysis.strategy` | single | `local`／`single` | `single` 每張照片最多一次圖片模型請求 | 否 |
| `analysis.stage_two_threshold` | 65 | 舊版讀取相容欄位 | 新工作不再使用，不會觸發第二次圖片請求 | 否 |
| 本機預篩選 | 啟用 | 截圖／明顯低品質可分別停用 | 排除項目 0 Token；不刪原檔 | 否 |
| `analysis.prefilter_sensitivity` | conservative | conservative／balanced／aggressive | 越積極越省 Token，也越可能誤排除 | 否 |
| `analysis.e6_prefilter_enabled` | true | true／false | 關閉後不會因六色量化失真而省下模型請求 | 否 |
| `analysis.e6_min_score` | 25 | 0–100，建議 20–35 | 越高越省 Token，但可能排除原圖好看、六色表現較弱的照片 | 否 |
| `analysis.scoring_rules` | 內建完整規則 | 100–12000 字元 | 影響新分析結果 | 否 |
| 綜合排序權重 | 50／20／10／20 | 四項合計 100% | 影響新分析與自動選片順序 | 否 |
| 最愛照片加分 | 5 | 0–30 | 只加入綜合排序分 | 否 |
| `analysis.concurrency` | 1 | 1–8，Intel N100 建議 1；確認 RSS 後最多先試 2 | 過高觸發限流／圖片記憶體尖峰 | 否 |
| `worker.queue_multiplier` | 1 | 1–4，N100 建議 1 | 增加記憶體中 Future | 否 |
| `worker.poll_seconds` | 15 | 1–60；低待機可設 30–60 | 越小待機喚醒越多 | 否 |
| `worker.progress_items` | 50 | 5–10,000 | 越小 Docker Log 越多 | 否 |
| `worker.progress_seconds` | 300 | 30–3,600 | 越小 Docker Log 越多 | 否 |
| `scheduler.poll_seconds` | 60 | 30–3,600 | 越小 SQLite／CPU 喚醒越多 | 否 |
| `offline.server_prefetch_margin_minutes` | 15 | 0–60 分鐘 | 太短可能來不及完成 Enhanced Slot 渲染 | 否 |
| `offline.future_schedule_prepare_hour_local` | 20 | 0–23 點 | 裝置本地到達此時後準備明日；過早會增加未來快照保留 | 否 |
| `analysis.max_retries` | 3 | 0–10 | 重試增加成本 | 否 |
| `model.analysis_model` | gpt-4o | 支援圖片／Schema 的模型 | 能力不足會進錯誤佇列 | 否 |
| `model.low_model`／`model.high_model` | 舊值 | 舊版讀取相容欄位 | 新工作不會恢復低／高兩次圖片請求 | 否 |
| `budget.daily_warning` | 5 | ≥0 美元 | 只警告 | 否 |
| `budget.daily_stop` | 10 | ≥0 美元 | 達到即停新請求 | 否 |
| `budget.monthly_warning` | 50 | ≥0 美元 | 只警告 | 否 |
| `budget.monthly_stop` | 100 | ≥0 美元 | 達到即停新請求 | 否 |
| `budget.job_default` | 10 | ≥0 美元 | 工作達到後暫停 | 否 |
| `budget.photo_max` | 0.25 | ≥0 美元 | 過低會阻擋單次模型分析 | 否 |
| `budget.max_tokens` | 8000 | 256–1,000,000 | 需符合模型能力 | 否 |
| `render.memory_threshold` | 70 | 0–100 | 過高可能無候選 | 否 |
| `render.quantity` | 5 | 1–50 | 增加下載量 | 否 |
| `render.selection_mode` | history_today | history_today／top_ranked | 歷年今日會依系統時區與 EXIF 拍攝日選片 | 否 |
| `render.history_today_window_days` | 7 | 0–31 | 0 只接受完全相同月日 | 否 |
| `render.history_today_fallback` | nearby_then_ranked | nearby_then_ranked／nearby_only／ranked／none | 限制越嚴格越可能沒有足量候選 | 否 |
| `render.e6_weight` | 20 | 0–60% | 過高會讓面板顯示效果凌駕回憶分 | 否 |
| `render.layout` | photo_info | full／postcard／photo_info／photo_pair／photo_pair_caption／adaptive_memory／calendar／weather_sensor | 日曆與天氣版型的照片區較小 | 否 |
| `render.show_capture_date` | true | true／false | EXIF 日期錯誤時也會跟著顯示 | 否 |
| `render.font_path` | 內建芫荽 | 內建手寫／文青風格或已上傳 TTF／OTF／TTC | 缺字會停止發布，不會 fallback | 否 |
| `render.show_location` | true | true／false | 只顯示最近城市，不顯示座標 | 否 |
| `render.location_max_distance_km` | 80 | 1–500 公里 | 過大可能顯示不準確的鄰近城市 | 否 |
| `render.profile` | gdep073e01_6c | 四色／GDEP 六色／GDEY 七色 | 必須與裝置面板相符 | 否 |
| `render.dither` | gooddisplay | 原廠相容／照片平滑／Floyd／Atkinson／Bayer／none | 照片平滑可能柔化極細線；兩種新模式強度固定 | 否 |
| `render.dither_strength` | 1 | 0–2 | 過高會增加色點 | 否 |
| `render.color_distance` | oklab | oklab／rgb | 切換會改變色彩映射 | 否 |
| `render.weather_enabled` | false | 啟用前先填正確經緯度 | 需連外；失敗不阻擋照片發布 | 否 |
| 天氣經緯度／顯示名稱 | 臺北市中心／所在地 | 緯度 -90–90、經度 -180–180 | 預設座標只是範例，啟用前必須修改 | 否 |
| `render.sensor_device_id` | 空白 | PhotoPainter 裝置 ID；空白取最近回報 | 多裝置時可能抓到別的房間 | 否 |
| `device.legacy_api_enabled` | false | 歷史相容設定；舊 Web 路由已移除 | 不能用此鍵恢復 URL 金鑰下載路由 | 不適用 |
| `device.default_timezone` | Asia/Taipei | IANA 時區 | 影響新增裝置排程 | 否 |
| `device.default_schedule` | 08:00 | 00:00–23:59 | 影響新增裝置刷新時間 | 否 |
| `device.default_rotation` | 0 | 0／180 | 目前 7.3 吋正式韌體限制 | 否 |
| `device.default_panel_profile` | gdep073e01_6c | 四色／GDEP 六色／GDEY 七色 | 型號錯誤會由韌體拒絕 | 否 |
| 離線／恢復通知 | 30 小時／啟用 | 1–720 小時；掃描預設 300 秒 | 需大於裝置刷新週期 | 否 |
| 離線重複提醒 | 停用／冷卻 24 小時 | 1–720 小時 | 過短會造成通知轟炸 | 否 |
| Webhook | 停用 | 預設只允許 HTTPS URL、2–30 秒逾時 | 只連可信端點；Token 加密保存 | 否 |
| `system.log_level` | INFO | DEBUG／INFO／WARNING／ERROR／CRITICAL | DEBUG 增加磁碟寫入 | 否 |
| `system.log_format` | json | human/json | 集中 Log 建議 json | 否 |
| `system.diagnostics_cache_seconds` | 21600（6 小時） | 30–86,400 | 太小會反覆掃大型縮圖目錄 | 否 |
| `security.session_minutes` | 30 | 5–1440 | 過長增加共用裝置風險 | 否 |
| `backup.schedule_enabled` | true | true/false | 關閉後需手動備份 | 否 |
| `backup.hour` | 3 | 0–23 | 避開大量分析 | 否 |
| `backup.retention` | 14 | 1–365 | 過低縮短回復期 | 否 |

所有修改寫入 `setting_history`，最近 100 筆直接顯示在設定頁；Secret 永不寫入摘要。Web、Worker、排程、Log 與 Session 的新設定均動態生效。部署層 Cookie、Proxy、Port 與掛載仍需依部署流程重新啟動。

新安裝的照片庫 `incremental_scan` 預設在每月 1 日 03:00 執行；`full_reconcile` 預設在每年 1 月 1 日 04:00 執行。這是為大型 NAS 照片庫降低 traversal、磁碟喚醒與 SQLite 活動的低頻政策，不是每日即時相簿。升級不會覆蓋既有管理員自訂 cron；需要立即納入照片或人工核對時，仍可從維護頁建立增量掃描或完整掃描工作。

新工作只有 `local` 與 `single` 兩個正式策略。`low_cost`、`smart`、`smart_two_stage`、`high_quality` 與 `single_high` 仍可讀取舊工作或舊 API payload，但會正規化為單次完整分析；不會再執行低成本→高品質的第二次圖片上傳。`analysis.caption_variants_enabled` 仍可讀取舊設定，但已移到進階設定，新的基本設定頁不再突出顯示五種候選文案。

## Web 與部署設定的邊界

不需要修改 Python。分析、排程、模型、成本、渲染、裝置、Log 層級、Session 與備份都由 Web 控制。Enhanced PhotoPainter 的 `offline.server_prefetch_margin_minutes` 預設為 15 分鐘；`offline.future_schedule_prepare_hour_local` 預設為裝置本地 20:00，之後 Scheduler 會預先準備明日排程。兩者都必須保留足夠渲染與網路緩衝，不應改成造成裝置高頻輪詢的值。宿主機 Volume、Port、映像 Tag、HTTPS Secure Cookie、Docker CPU／RAM／PID 上限與 logging driver 必須在容器啟動前由 `.env`／Compose 決定；容器內程式不應取得 Docker socket 去改寫宿主機。設定頁會只讀顯示目前部署資訊。

### PhotoPainter Enhanced 排程操作邊界

- `inktime_offline_schedule` 一律搭配 `offline_prefetch_allowed=true`；`legacy_online` 與 `stock_compat` 一律是 `false`。建立或 PATCH 省略 prefetch 欄位時由模式自動正規化；明確矛盾值回 `400 DEVICE-008`。
- `schedule_not_ready` 的 `retry_after_epoch` 會留在今日剩餘 Slot 之前；`next_slot_epoch` 可供韌體再次驗證。今日沒有剩餘 Slot 才會回明日第一個 prepare point，不能把 07:50 的今日 08:00／12:00 排程直接跳到明日。
- 本地 20:00 後，今日若沒有 `slot.show_at > local_now`，Scheduler 只建立 tomorrow；今日仍有未來 Slot 才同時確保 today + tomorrow。重複 tick 依 dedupe key 不新增重複工作。
- `local_next` 是無網路的本地預覽；每按一次依 NVS 的 `preview_schedule_id`／`preview_slot_index` 循環下一張，重複 SHA 會跳過。它不消耗正式 Slot、不改 Queue／current/LKG、不寫 terminal ACK 或 ACK journal；正式 timer wake 不受此 cursor 影響。

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

八種版型為全版照片、明信片、照片＋日期地點、純雙照片、雙照片各自一句話、智慧自適應回憶、月曆相框、天氣＋室內溫溼度；後兩種只支援直向。預覽可暫時切換版型，按「設為預設版型」才會改正式發布設定。天氣資料為選用功能，從 Open-Meteo 取得目前天氣、溼度與當日高低溫並快取 30 分鐘；外部服務失敗時照片仍正常發布。室內資料來自 PhotoPainter 裝置狀態回報；沒有感測值時畫面會明確顯示尚無回報。

## 歷年今日選片

預設 `history_today` 不是單純挑最高分：依 `general.timezone` 的今天，先找「月、日相同且年份早於今年」的照片；不足時在預設前後 7 日內依日期距離補足，再依綜合排序回退。可把回退改成只接受鄰近日、直接採排名或完全不補圖，也可切換 `top_ranked`。手動指定不使用自動排名，但仍須符合 active／eligible、Library、原始檔、隱私與當前執行模式的來源資格。

## 本機預篩選與 ExifTool 邊界

照片掃描只建立 JPG、PNG、WebP、HEIC／HEIF、TIFF、BMP 等靜態照片；MOV、MP4、M4V、MKV、WebM 與 GIF 動畫會計入 `excluded_videos` 後停止，不會建立模型工作。雲端分析前再依本機檔名、尺寸、格式、相機 EXIF、模糊、對比、曝光與解析度判斷：截圖達門檻即可排除；一般照片必須同時出現至少兩項明顯缺陷才排除。人工標記為最愛的照片永遠略過此預篩選。

ExifTool 能提供 MIME、相機、軟體、拍攝時間與 GPS 等中繼資料，但不能可靠判斷構圖、人物表情或「好不好看」。目前正式流程直接用 Pillow 讀取 EXIF／GPS，不要求容器安裝 ExifTool；畫質則以本機縮圖特徵判斷。這可避免每張照片額外啟動外部程序，也不會把照片或座標傳到第三方服務。

## ESP32 遠端設定

新自製 ESP32 的 AP 配對只填 Wi-Fi、InkTime URL 與 TLS Root CA；裝置首次連線會在實體面板顯示短效配對碼，管理員在「裝置」頁輸入該碼並核准後，由裝置以可恢復 claim／confirm 領取 Device Secret。confirm 前不建立正式裝置資料列。之後從「裝置」編輯每台 ESP32 的名稱、啟停、面板 Profile、IANA 時區、每日 `HH:MM` 與 0°／180°；下一次取得 Manifest 自動套用。既有 Legacy 裝置仍使用相容 Bearer Token，Stock PhotoPainter 走 `/dataUP` 分流。裝置頁以期望版本／ACK 區分「已儲存」與「裝置已生效」，並顯示離線狀態、通知、firmware、RSSI、free heap／PSRAM、下載計數與最後錯誤。完整協定見[ESP32 自動配對與憑證生命週期](../devices/ESP32_AUTOMATIC_PAIRING_ZH_TW.md)。

## 裝置能源儀表板

「能源」頁可依裝置與 7／30／90／365 天期間查看裝置自動回報的電池百分比、電壓、
刷新耗時、完整喚醒時間與最近樣本。平台保留最近 400 天低頻遙測；這些值只供診斷，
不會阻擋下載或刷新。頁面沒有需要管理員以外接儀器取得的容量／電流欄位，也不提供
依賴人工量測的續航模型。

## 照片評分與門檻

模型一次輸出回憶、美觀、技術品質與情緒四個 0–100 原始分數。系統另用「評分」頁的四項權重算出 `ranking_score`，並在最愛照片上加入設定的額外分數；原始四項分數不會被覆寫。`analysis.stage_two_threshold` 僅保留舊設定讀取相容，`render.memory_threshold` 才是電子紙候選的最低回憶分門檻。

- 改模型：在「設定」調整 `model.analysis_model`，並在「模型」頁設定 Provider。
- 舊版 `model.low_model`／`model.high_model` 與 `analysis.stage_two_threshold` 僅供相容讀取，不會恢復第二次圖片請求。
- 改電子紙最低回憶分：調整 `render.memory_threshold`。
- 改模型評分規則或綜合權重：到「評分」頁儲存為新版本；下一次分析立即生效，既有照片不會自動重算。
- 測試照片：在「評分」頁選一張照片並確認付費請求；暫存檔會在請求結束後刪除，Token、費用與延遲仍寫入成本紀錄。
- 還原：版本歷史的「還原此版本」會建立一個新的目前版本，不會刪除或覆寫任何歷史。
- 評分預設位於 `inktime/app/domain/analysis/scoring.py`，並透過版本化規則保存修改歷史。
- JSON Schema、繁體中文與不得虛構等固定約束不允許從網頁覆寫，位於 `inktime/app/providers/openai_compatible.py`。

完整流程圖與程式入口見 [專案架構與評分流程](../architecture/ARCHITECTURE_ZH_TW.md)。

## 排程換圖與不合格照片

`display_prepare` 支援且只支援 `display_times`、`lead_minutes`、`daily_count`、`device_ids`、`candidate_years`、`prefetch_count`、`ai_fallback`、`render_fallback`。未知欄位不會被靜默忽略。`device_ids` 解析為實際啟用裝置的 Profile；`daily_count × prefetch_count` 決定候選數量；年份會直接限制 SQL 候選。

人工排除、自動排除、Missing、deleted、路徑逃逸、原始檔缺失的照片均不能正式發布；`automatic_ai` 另要求有效分析，允許本機來源的模式可使用已完成 Scanner 特徵的照片。管理員明確指定這類照片會收到 `RENDER-009`，系統不會換成另一張照片。

## PhotoPainter 跨日操作檢查

- API 只使用 `/api/device/v1/offline-schedule?target=current` 或 `?target=next`；不要傳任意日期、`+N` 或 history。current 回應的 `next_target_start_epoch` 與 `next_schedule_prefetch_epoch` 是 server 以 IANA timezone 計算的明日技術截止。
- Scheduler 在本地 `offline.future_schedule_prepare_hour_local` 與「明日第一 Slot 減 `prefetch_lead_minutes` 與 `offline.server_prefetch_margin_minutes`」兩者較早者到達時準備 tomorrow；today 若仍有晚間 future Slot，兩者可以同時存在。
- 裝置端先把 tomorrow 寫入 staged-next，不覆寫 active；午夜 RTC promote 前不套用 future rotation、按鍵或 schedule config。只在 promote 後將 00:00 Slot 視為 formal display。
- `MANIFEST_RECEIVED` 到 `HASH_VERIFIED` 只表示 prefetch，不能當成顯示完成；實際面板刷新前不送 display terminal ACK。`local_next` 只做本地預覽並保留 retry state。
