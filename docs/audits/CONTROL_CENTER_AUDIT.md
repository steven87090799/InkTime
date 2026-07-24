# InkTime 設定控制中心治理稽核

稽核日期：2026-07-24
Repository：`steven87090799/InkTime`
稽核 Base：`98e788fcb907b9cc80d3c9e723237779b4a748e7`
範圍：執行當下的 `origin/main`，包含 PR #23（Caption Controls）與 PR #24（Observability Console）。

## 1. 結論

本次先稽核、再建立治理基礎，沒有重新實作 PR #23 或 PR #24。現況有單一
`SETTING_DEFINITIONS`，共 124 個設定；118 個原本會出現在 `/settings`，6 個由既有
`/scoring` 控制中心管理。靜態 runtime 路徑確認 117 個 Key 有直接讀取，7 個 Key
雖可儲存但沒有直接 runtime 消費，屬「假設定」風險。

| 指標 | 數量 | 說明 |
|---|---:|---|
| `SETTING_DEFINITIONS` | 124 | 唯一設定 Schema，不另建第二套 |
| `/settings` 可顯示 | 118 | 排除 6 個既有評分控制中心 Key |
| Runtime 有直接讀取 | 117 | 依 `inktime/` 靜態引用與服務路徑核對 |
| Runtime 未直接讀取 | 7 | 列於第 4 節，不宣稱可動態生效 |
| 動態生效 | 66 | 含敏感位置與高風險項目；不代表都適合基本模式 |
| 適合日常動態開放 | 54 | 排除高風險、敏感、未接 Runtime 與專屬控制中心 Key |
| 下次工作生效 | 57 | 分析、模型、Worker、Scanner、預算 |
| 需重啟 | 1 | `device.legacy_api_enabled`，且目前未接 runtime |
| 低／中／高風險 | 68／24／32 | 高風險在進階模式，並維持白名單與上限 |
| 影響 Cache／重分析／重渲染 | 19／26／26 | 是影響提示，不會自動重跑 |
| 精確位置敏感 Key | 4 | 不進 Snapshot／Export |
| 私人路徑狀態 Key | 1 | `render.font_path` 只保留是否設定，不輸出內容 |
| 額外硬編碼治理候選 | 38 | 第 5 節 H01–H38 |
| 部署／安全／硬體禁止開放類別 | 22 | 第 6 節；只讀或留在部署層 |

## 2. 分類判準

| 代碼 | 類別 | 治理方式 |
|---|---|---|
| A | 可安全動態調整 | Web 可部分更新，立即生效 |
| B | 可調整但只影響新工作 | 不回寫舊分析，不自動重跑 |
| C | 可調整但需要重啟 | UI 明示，儲存後不假裝已套用 |
| D | 部署邊界，只能唯讀顯示 | Port、掛載、容器資源等 |
| E | 敏感密鑰 | 只進既有 Secret Store |
| F | 硬體／協議不變條件 | 固定尺寸、BIN、SHA、腳位 |
| G | 不應開放的危險設定 | 任意 SQL／Shell／Python、無上限資源等 |
| H | 已存在但 UI／驗證不足 | 本 PR 的主要治理範圍 |
| I | 已存在但可能未真正生效 | UI 必須標示「尚未接上 Runtime」 |
| J | 未來功能 | 只列 Roadmap，不宣稱完成 |

影響欄縮寫：`C`=Cache Fingerprint、`A`=重分析、`Rk`=只重算排序、
`Re`=重新渲染、`D`=允許既有裝置／Job／Preview 覆寫。`—` 表示沒有直接影響。

## 3. 既有設定完整盤點

下表以功能族群列出全部 124 個 Key。每一列的「Key 範圍」就是該族群完整清單；
每個 Key 的型別、預設、min/max/choices、中文名稱、safe fallback、dependency、
conflict、validation group 與 impact 均由同一個 `SETTING_DEFINITIONS` 在 runtime
產生，`GET /api/v1/settings/metadata` 可取得逐 Key 完整資料。

| 分類 | Key 範圍 | 現況（Schema／Web／Runtime） | 建議名稱與型別／範圍 | Fallback／風險／生效 | 影響／敏感／優先級／PR |
|---|---|---|---|---|---|
| 一般 | `general.timezone` | 是／是／是 | 系統時區；IANA timezone choice | `Asia/Taipei`；中；動態 | 排程跨日；非敏感；P0；本 PR |
| Observability Debug | `observability.debug_enabled`, `debug_level`, `debug_components`, `debug_auto_disable_minutes` | 是／是／前兩項部分 | 暫時 Debug、層級、元件、1–1440 分 | 安全預設關閉；中；動態 | —；診斷內容需遮蔽；P0；本 PR＋PR-OBS |
| Activity 保留 | `activity_retention_days`, `debug_retention_hours`, `activity_max_rows`, `activity_poll_seconds`, `stuck_job_minutes` | 是／是／poll 未接 | 重要事件 7–365 天、DEBUG 1–168 小時、1k–500k 列、3–60 秒、1–120 分 | 原預設；中；動態 | 未解決錯誤保護；P0；本 PR＋PR-OBS |
| 分析策略 | `analysis.strategy`, `stage_two_threshold` | 是／是／是 | 新工作策略 choice；0–100 | `smart_two_stage`／65；中；下次工作 | A；非敏感；P1；PR-AI |
| Caption 開關 | `advanced_caption_enabled`, `caption_variants_enabled` | 是／是／是 | 進階文案、候選版本；bool | 關閉；中；下次工作 | C+A；P0；本 PR Metadata |
| Caption 長度 | `caption_min_chars`, `caption_target_chars`, `caption_max_chars`, `side_caption_min_chars`, `side_caption_target_chars`, `side_caption_max_chars` | 是／是／是 | 詳細 0–1000、短文 0–120；整數 | PR #23 預設；中；下次工作 | C+A；`min≤target≤max`；P0；本 PR |
| Caption 風格 | `copy_default_style`, `copy_humor_level`, `copy_poetic_level`, `copy_avoid_cliche`, `copy_avoid_direct_description`, `copy_forbid_exclamation`, `copy_forbid_like_phrase`, `copy_max_commas`, `copy_avoid_abstract_ending` | 是／是／是 | 中文友善風格；choice/bool/0–10 | PR #23 預設；中；下次工作 | C+A；P1；PR-CAPTION |
| Caption 規則文字 | `copy_banned_words`, `copy_banned_patterns`, `copy_custom_rules` | 是／是／是 | 多行文字，有長度上限 | 原預設；高；下次工作 | C+A；不可接受程式碼；P1；PR-CAPTION |
| AI 模式與配額 | `ai_mode`, `ai_top_n`, `ai_daily_photo_limit`, `ai_monthly_photo_limit` | 是／是／是 | mode choice；1–10k／100k／1m | `top_candidates`, 50/50/500；高；下次工作 | A；成本風險；P1；PR-AI |
| 旅行與地點排序 | `travel_bonus_enabled`, `home_latitude`, `home_longitude`, `home_radius_km`, `travel_bonus_near`, `travel_bonus_far`, `foreign_country_bonus`, `rare_location_bonus`, `max_total_bonus`, `location_rule_version` | 是／是／是 | bool、GPS 範圍、0–40 分、版本字串 | 原預設；中／位置高；動態 | Rk；精確 GPS 不匯出；P1；PR-SELECTION |
| 本機預篩選 | `prefilter_enabled`, `prefilter_screenshots`, `prefilter_low_quality`, `prefilter_sensitivity`, `e6_prefilter_enabled`, `e6_min_score` | 是／是／是 | bool、sensitivity choice、0–100 | 保守模式；中；下次工作 | A；不刪照片；P1；PR-SELECTION |
| 評分控制中心 | `scoring_rules`, 四個 `ranking_*_weight`, `ranking_favorite_bonus` | 是／不在 `/settings`／是 | 既有 `/scoring` 版本化 UI | 原設定；高；下次工作 | rules=A；weights=Rk；P1；PR-SELECTION |
| Worker／Scanner | `analysis.concurrency`, `analysis.max_retries`, `worker.queue_multiplier`, `worker.poll_seconds`, `worker.progress_items`, `worker.progress_seconds`, `scanner.disk_batch_size`, `scanner.write_batch_size`, `scanner.missing_threshold_percent`, `scheduler.poll_seconds` | 是／是／是 | 全部有界；1–8、0–10、100–10k 等 | 低資源預設；高；下次工作 | —；不可無上限；P1；PR-SCHEDULE |
| 模型 | `model.low_model`, `model.high_model` | 是／是／是 | 文字，目前未綁 Provider 模型白名單 | `gpt-4o-mini`／`gpt-4o`；高；下次工作 | C+A；P1；PR-AI |
| 預算 | `budget.daily_warning`, `daily_stop`, `monthly_warning`, `monthly_stop`, `job_default`, `photo_max`, `max_tokens` | 是／是／部分 | 有界數字；warning≤stop | 原預設；高；下次工作 | A／成本；P1；PR-AI |
| Renderer 選片 | `render.memory_threshold`, `quantity`, `selection_mode`, `history_today_window_days`, `history_today_fallback`, `e6_weight` | 是／是／是 | 0–100、1–50、安全 choice | 原預設；中；動態 | Rk+Re；P1；PR-SELECTION／RENDER |
| Footer | `caption_wrap_enabled`, `caption_max_lines`, `caption_min_font_size` | 是／是／是 | bool、1–2 行、12–32 px | 關閉／2／17；中；動態 | Re；關閉時保留值；P0；本 PR |
| Renderer 版型 | `layout`, `frame_orientation`, `fit_mode`, `show_capture_date`, `font_path`, `show_location`, `location_max_distance_km` | 是／是／是 | 安全 choice/bool/0–500 km | 原預設；中；動態 | Re+D；P1；PR-RENDER |
| 天氣／感測 | `weather_enabled`, `weather_latitude`, `weather_longitude`, `weather_location_name`, `sensor_device_id` | 是／是／是 | bool、GPS、名稱、裝置 ID | 關閉；位置高；動態 | Re+D；GPS 不匯出；P2；PR-RENDER |
| Palette／Dither | `profile`, `dither`, `dither_strength`, `color_distance`, `custom_photo_presets` | 是／是／是 | Profile／Palette／Dither 安全白名單；0–2 | `safe_4c` 等；中；動態 | Re+D；不開放尺寸/BIN；P1；PR-RENDER |
| 裝置預設 | `device.default_timezone`, `default_schedule`, `default_rotation`, `default_panel_profile`, `legacy_api_enabled` | 是／是／前四項 | IANA、HH:MM、0/180、Profile choice、bool | 安全預設；legacy 高；動態／重啟 | D；legacy 未接；P1；PR-DEVICE |
| 裝置通知 | `notification.device_offline_enabled`, `device_offline_hours`, `device_recovery_enabled`, `device_offline_repeat_enabled`, `device_offline_cooldown_hours`, `scan_seconds` | 是／是／是 | bool、1–720 hr、60–3600 秒 | 原預設；中；動態 | —；P1；PR-NOTIFY |
| Webhook | `notification.webhook_enabled`, `webhook_url`, `webhook_timeout_seconds` | 是／是／是 | bool、無帳密 HTTP(S)、1–60 秒 | 關閉；高；動態 | Token 另存 Secret Store；P1；PR-NOTIFY |
| Log／診斷 | `system.log_level`, `log_format`, `diagnostics_cache_seconds` | 是／是／是 | level/format choice、30–3600 秒 | INFO/json/300；中；動態 | —；不記秘密；P1；本 PR |
| Session | `security.session_minutes` | 是／是／是 | 5–1440 分 | 30；高；動態 | Auth 不可關閉；P0；本 PR |
| 備份 | `backup.schedule_enabled`, `backup.hour`, `backup.retention` | 是／是／是 | bool、0–23、1–365 份 | true/3/14；中；動態 | 不刪目前必要備份；P1；PR-STORAGE |

## 4. 已存在但未真正接上 Runtime

| Key | 現況 | 安全處置 | 後續 PR |
|---|---|---|---|
| `observability.debug_level` | 可儲存，但事件寫入只判斷 `debug_enabled` | UI 顯示「尚未接上 Runtime」；不得宣稱 detailed 有效 | PR-OBS |
| `observability.debug_components` | 可儲存，`record()` 未依元件過濾 | 同上；仍維持全域遮蔽 | PR-OBS |
| `observability.activity_poll_seconds` | Activity 頁仍固定 `5000 ms` | 同上；後續由 server 安全注入 3–60 秒 | PR-OBS |
| `budget.daily_warning` | BudgetService 只讀 stop | 只顯示告警規格，未接通知前不宣稱有效 | PR-AI |
| `budget.monthly_warning` | 同上 | 同上 | PR-AI |
| `budget.job_default` | 建立 Job 未統一套用 | 先標示未接，避免誤以為每工作預算已限制 | PR-AI |
| `device.legacy_api_enabled` | 無直接 runtime gate | 維持關閉；未完成前不可啟用 legacy API | PR-DEVICE |

## 5. 額外硬編碼治理候選（38）

這些不是「看到數字就全部做成設定」。Protocol、查詢界線與保護上限保留為程式安全契約；
只有適合日常微調者才進後續 PR。

| ID／分類 | 檔案與符號 | 現值 | 建議中文名稱／Key／型別與範圍／Fallback | Schema／Web／Runtime | 風險／生效／影響／敏感／優先級／PR |
|---|---|---|---|---|---|
| H01 I | `activity.html:setInterval` | 5 秒 | Activity 輪詢／既有 `activity_poll_seconds`／int 3–60／5 | 是／是／否 | 中／動態／—／否／P0 PR-OBS |
| H02 B | `ObservabilityService._PROVIDER_WINDOW` | 15 分 | Provider 告警視窗／`observability.provider_window_minutes`／5–120／15 | 否／否／是 | 中／動態／—／否／P1 PR-OBS |
| H03 B | `_check_providers` | 3/8 次 | Provider ERROR/CRITICAL 次數／兩個 int 1–20／3/8 | 否／否／是 | 高／動態／—／否／P1 PR-OBS |
| H04 B | `_check_providers` | Queue 15 分 | Provider 冷卻卡住／`provider_cooldown_stuck_minutes`／5–180／15 | 否／否／是 | 中／動態／—／否／P1 PR-OBS |
| H05 B | `_check_releases` | staged 20 分 | Release 卡住時間／`release_stuck_minutes`／5–240／20 | 否／否／是 | 中／動態／—／否／P1 PR-OBS |
| H06 F | `_check_releases` | LIMIT 100 | Release 掃描上限／安全常數 10–500／100 | 否／否／是 | 低／重啟／—／否／P3 不開放 |
| H07 F | `_check_releases` | known IDs 1000 | Recovery 掃描界線／安全常數／1000 | 否／否／是 | 低／重啟／—／否／P3 不開放 |
| H08 B | `_check_schedules` | max(300, timeout) | 排程 Grace／`schedule.grace_seconds`／300–7200／300 | 否／否／是 | 中／動態／—／否／P1 PR-SCHEDULE |
| H09 B | `_device_due` | 排程後 2 小時 | 裝置下載 Grace／`device.download_grace_hours`／1–24／2 | 否／否／是 | 中／動態／D／否／P1 PR-DEVICE |
| H10 B | `_device_due` fallback | 指派後 30 小時 | 無效排程 fallback／安全常數／30 | 否／否／是 | 高／重啟／D／否／P3 不開放 |
| H11 B | `_check_devices` | Verify 2 小時 | Payload Verify ACK 門檻／`device.verify_ack_hours`／1–24／2 | 否／否／是 | 高／動態／D／否／P1 PR-DEVICE |
| H12 B | `_check_devices` | Display 2 小時 | Display ACK 門檻／`device.display_ack_hours`／1–24／2 | 否／否／是 | 高／動態／D／否／P1 PR-DEVICE |
| H13 B | `_check_devices` | 連敗 3 次 | 裝置連敗門檻／`device.failure_alert_count`／1–20／3 | 否／否／是 | 高／動態／D／否／P1 PR-DEVICE |
| H14 F | `_check_devices` | LIMIT 200 | 裝置監控查詢界線／安全常數／200 | 否／否／是 | 低／重啟／—／否／P3 不開放 |
| H15 F | `_check_devices` | 每裝置 20 事件 | ACK 查詢界線／安全常數／20 | 否／否／是 | 低／重啟／—／否／P3 不開放 |
| H16 B | `_ALERT_INTERVAL` | 5 分 | 告警去重冷卻／`observability.alert_cooldown_minutes`／1–60／5 | 否／否／是 | 中／動態／—／否／P1 PR-OBS |
| H17 F | `_BATCH_SIZE` | 500 | Activity 清理批次／安全上限 100–2000／500 | 否／否／是 | 高／重啟／—／否／P3 不開放 |
| H18 A | `ObservabilityService._check_platform` | 磁碟 85/95% | 檔案系統 WARNING/CRITICAL／兩個 number 50–99／85/95 | 否／否／是 | 高／動態／—／否／P1 PR-STORAGE |
| H19 B | `ProviderRouter.failure_threshold` | 3 | Provider Circuit 失敗門檻／`provider.failure_threshold`／1–10／3 | 否／Provider UI 部分／是 | 高／新工作／—／否／P1 PR-AI |
| H20 B | `ProviderChannel.cooldown_seconds` | 300 | Provider 冷卻／既有 Provider 欄位／1–86400／300 | Provider Schema／是／是 | 高／新工作／—／否／P1 PR-AI |
| H21 B | `OpenAICompatibleProvider` | timeout 120 秒 | Connect/Read Timeout／拆成兩欄 5–600／10/120 | Provider Schema 部分／是／是 | 高／新工作／—／否／P1 PR-AI |
| H22 F | `OpenAICompatibleProvider.submit_batch` | 50,000 requests | Batch 單批上限／Protocol 安全常數／50k | 否／否／是 | 高／重啟／A／否／P3 不開放 |
| H23 F | `submit_batch` | 200 MB | Batch JSONL 上限／Protocol 安全常數／200 MB | 否／否／是 | 高／重啟／A／否／P3 不開放 |
| H24 J | Batch Provider API | completion window 24h | Batch 完成視窗／未完成生命週期，不開 UI | 介面／否／只有 submit/poll/cancel | 高／未支援／A／否／P1 PR-AI-BATCH |
| H25 F | `analysis._model_call` | reservation sleep 0.05 秒 | Single-flight 等待節奏／內部安全常數 | 否／否／是 | 低／重啟／C／否／P3 不開放 |
| H26 B | `analysis` JSON repair | 1 次 | JSON 修復次數／`analysis.json_repair_attempts`／0–2／1 | 否／否／是 | 高／新工作／C+A／否／P2 PR-AI |
| H27 F | `rendering` candidate query | LIMIT 250 | 選片查詢批次／安全界線／250 | 否／否／是 | 中／重啟／Rk／否／P3 不開放 |
| H28 F | `rendering._history_rows` | LIMIT 500 | 歷史查詢批次／安全界線／500 | 否／否／是 | 中／重啟／Rk／否／P3 不開放 |
| H29 F | `rendering` calibration | 前 40 筆 | 相對校準樣本／架構參數 10–100／40 | 否／否／是 | 中／新工作／Rk／否／P2 PR-SELECTION |
| H30 B | `weighted_history_choice` | top_n ≤100 | 隨機歷史候選／`selection.weighted_top_n`／1–100／10 | Job override／否／是 | 中／新工作／Rk／否／P2 PR-SELECTION |
| H31 F | `rendering._draw_footer` | 最多 2 行 | Footer 實體版面上限／既有 max_lines capped 2 | 是／是／是 | 高／動態／Re／否／P0 保留上限 |
| H32 B | `runner.cleanup` | 5 GiB | Thumbnail 最大占用／`storage.thumbnail_max_gib`／0.25–50／5 | Job payload／Maintenance 部分／是 | 高／新工作／—／否／P1 PR-STORAGE |
| H33 B | `runner.cleanup` | 30 天 | Thumbnail 保留／`storage.thumbnail_retention_days`／1–365／30 | Job payload／Maintenance 部分／是 | 中／新工作／—／否／P1 PR-STORAGE |
| H34 H | `TASK_DEFAULTS` | Scanner 02:00/4h/2 retry | Scanner 排程組合／既有 scheduled_tasks schema | DB／`/schedules`／是 | 高／下次排程／—／路徑敏感／P1 PR-SCHEDULE |
| H35 H | `TASK_DEFAULTS` | AI 預設 disabled | AI 排程生命週期／既有 scheduled_tasks schema | DB／`/schedules`／是 | 高／下次排程／A／否／P1 PR-SCHEDULE |
| H36 H | `TASK_DEFAULTS` | Display 07:30/08:00 | 換圖準備／既有 scheduled_tasks schema | DB／`/schedules`／是 | 高／下次排程／Re+D／否／P1 PR-SCHEDULE |
| H37 D | `platform.initialize_platform` | Cookie Secure env | Cookie Secure／部署環境 bool／安全 fallback | Env／唯讀／啟動讀取 | 高／重啟／—／E／P0 部署層 |
| H38 D | `Database` | writer timeout 10s | SQLite writer 安全界線／部署／5–60／10 | 程式／唯讀／啟動 | 高／重啟／—／否／P0 部署層 |

## 6. 永不開放任意前端輸入的 22 類邊界

1. Docker Port。
2. Volume 與照片掛載路徑。
3. Docker CPU、RAM、PID 上限。
4. 主密鑰。
5. Session Secret。
6. Provider API Key／Bearer Token／Webhook Token。
7. SQLite Migration 版本。
8. 任意 SQL。
9. 任意 Shell。
10. 任意 Python。
11. 480×800 實體面板尺寸。
12. BIN Pixel Format。
13. Manifest SHA／Payload SHA 驗證。
14. Firmware GPIO Pins。
15. 未知 Provider 程式碼。
16. 無上限並行。
17. 無上限 Retry。
18. 關閉 CSRF。
19. 關閉 Auth。
20. 關閉路徑驗證。
21. 關閉未解決 ERROR／CRITICAL 保留。
22. 未簽章 OTA 與任意韌體發布。

控制中心可唯讀顯示其中不敏感的狀態，但修改仍留在部署層、Secret Store 或程式安全契約。

## 7. PR #23／#24 相容性核對

| 已合併能力 | 納入治理方式 | 禁止事項 |
|---|---|---|
| Caption Controls／Variants | Metadata、dependency、跨欄位驗證、Cache/Reanalysis impact | 不改 Prompt、Schema、候選資料 |
| Footer 多行 | 關閉時 UI 隱藏行數／字體；保留儲存值 | 不改 renderer 上限與輸出協議 |
| Caption Cache Fingerprint | `cache_impact` 明示 | 不主動清 Cache 或重分析 |
| Activity／Debug 自動到期 | Metadata、Snapshot、partial update | 不增加待機 Activity 洪水 |
| Activity Retention／Stuck Detection | 保護未解決錯誤、限制範圍 | 不允許清除重大未解決事件 |
| Provider／Release／Schedule／Device 告警 | Impact 與 Roadmap | 不重建第二套告警狀態機 |
| 中央遮蔽 | Snapshot/Export 再加敏感位置排除 | 不回顯 Secret 或精確 GPS |
| Dashboard／CRITICAL Banner | 保持既有 route/template 契約 | 不在本 PR 重做 Dashboard |

## 8. 設定版本與資料過期架構

| 版本 | 會遞增的設定 | 過期範圍 | 安全重跑策略 |
|---|---|---|---|
| `analysis_config_version` | Provider、模型、Prompt Schema、預篩選 | 對應 analysis/cache row | 只標記受影響且尚未有新版本者；不自動全庫 |
| `ranking_config_version` | 四權重、最愛／旅行／最低分 | 排序 materialization | 只重算已有原始分數的列，不呼叫 AI |
| `caption_config_version` | Caption Prompt、長度、語氣、候選規則 | Caption/semantic JSON | 只對明確選取的過期項目重生 |
| `renderer_config_version` | Layout、Footer、Palette、Dither | Preview／Release render | Release 標示舊版；不自動發布 |
| `selection_config_version` | 比例、重複限制、歷史排除 | Selection decision | 模擬或下一個 Job 使用新版本 |

既有 Caption Cache Fingerprint 仍是內容可重用的主要鍵；版本欄用來說明資料是否過期，
不可取代內容 Hash。第一個 PR 只提供 Impact Metadata，沒有建立大規模重處理系統。

## 9. 後續獨立 PR Roadmap

### PR-SELECTION：選片控制中心與有界模擬

- 使用情境：調整四權重、最愛／旅行／罕見地點、最低分、類型與新舊／橫直／單雙圖比例、
  同日／地點／事件上限、最近顯示排除、永久未顯示加權、雙圖相似度與時間距離、敏感／截圖／
  文件／收據策略。
- 資料模型：`selection_config_version` 與只讀 simulation result；不建立 Release。
- 風險：比例互斥、最愛例外破壞安全排除、無界查詢。
- 隱私：只讀本機 metadata；不傳照片、不呼叫 AI。
- Migration：版本表與可選 simulation audit；不儲存圖片。
- 驗收：列出入選／淘汰原因、每項加權、before/after；固定 Count/Sample 上限。

### PR-AI：AI、成本與 Provider 治理

- 使用情境：第一／二階 Provider、模型、圖片尺寸、max_tokens、connect/read timeout、retry、
  backoff、並行、日／月照片與金額、單 Job 預算、fallback、cooldown、JSON repair、cache retention。
- 資料模型：擴充既有 Provider 與 Usage，不把 API Key 移出 Secret Store。
- 風險：成本失控、重試風暴、錯誤 Provider 宣稱可用。
- Migration：Provider timeout 拆欄、budget policy version。
- 前置：完整 Provider capability probing；可靠 pricing 才顯示成本。
- 驗收：硬上限、off 時零 Provider 呼叫、估不出成本顯示「無法估算」。
- Batch 現況：已有 submit/poll/cancel adapter，但沒有持久化背景 lifecycle、result reconciliation、
  recovery 與 UI；必須 feature flag 預設關閉，不得宣稱完整可用。

### PR-RENDER：Renderer 與 A/B Preview

- 使用情境：Layout、Orientation、Contain、單雙圖、Footer、日期／地點、Caption Style、
  Palette、Dither、Strength、色差、Preset。
- 不變條件：直向 480×800；橫向 800×480 排版後旋轉；Server Renderer 才能產正式 BIN。
- 資料模型：`renderer_config_version`、有界 preview artifact，不進正式 Release。
- 風險：Profile/BIN 不相容、Browser Canvas 與正式輸出漂移。
- 驗收：A/B 使用同一 Domain Renderer；不建立正式 Release。

### PR-SCHEDULE／PR-DEVICE：排程、能源與裝置

- 使用情境：Scanner／AI／Display／Backup 排程、時區、安靜時段、catch-up、最大延遲、
  有界並行／retry、低電量延後、USB／電池策略、離線與 ACK 門檻、playlist/group/canary/LKG。
- 資料模型：擴充 `scheduled_tasks`；device group/playlist 需新表與 config version ACK。
- 風險：排程重複、電池耗盡、錯誤群組大量發布。
- Migration：device groups、assignments、canary rollout state。
- 驗收：模擬時鐘、錯過補跑一次、LKG 保護、Canary 停止條件。
- OTA：簽章 OTA 完成前只顯示 Firmware Version、Compatibility、需人工升級。

### PR-STORAGE：儲存、清理與備份

- 使用情境：Thumbnail、AI Cache、Activity、Release、Test Release、Backup、raw/semantic JSON、
  Missing Photo、已解決錯誤、Snapshot 占用與 30/90 天預測。
- 資料模型：有界 storage samples；cleanup preview/audit。
- 風險：刪原始照片、LKG、未解決 ERROR/CRITICAL、必要 Snapshot。
- 顯示語意：分開 `/data` 所在檔案系統使用率、InkTime `/data` 目錄、Thumbnail、Backup、
  Release、DB/WAL；不得把整個磁碟占用歸因 InkTime。
- 驗收：先預覽可回收量；只清白名單資料；備份／還原含 Snapshot。

### PR-NOTIFY／PR-PRIVACY：通知與隱私

- 使用情境：INFO/WARNING/ERROR/CRITICAL × 站內/Webhook/Email/LINE/Telegram、冷卻、
  去重、安靜時段、Recovery、測試通知；敏感照片、GPS 傳模、顯示精度、診斷路徑、
  Library policy、Raw Response 保留。
- 資料模型：notification routing matrix 與 privacy policy version；credential 仍在 Secret Store。
- 風險：秘密外洩、通知風暴、位置去匿名化。
- Migration：channel config（不含 Secret）與 delivery audit。
- 驗收：未實作通道 flag 關閉且標「尚未支援」；Snapshot/Diff/Export 無秘密。

### 其他獨立項目

| 項目 | 使用情境／資料模型 | 安全、隱私、效能風險 | Migration／前置／驗收 |
|---|---|---|---|
| 重複／相似照片工作台 | duplicate group review/action audit | 誤刪原檔、巨量比較 | group decision table；先唯讀、所有刪除另案 |
| 拖拉式版型編輯器 | versioned layout DSL | 任意座標破版、注入 | 白名單 DSL；Server Renderer golden tests |
| 面板色彩校正 Profile | signed/calibrated profile | 錯色、BIN 不相容 | profile version；實體面板量測驗收 |
| Provider 成本趨勢 | aggregate usage samples | 價格過期、洩漏模型輸入 | pricing version；帳單交叉核對 |
| 完成時間預估 | bounded rolling statistics | 誤導、慢查詢 | aggregate table；顯示信賴區間 |
| Prometheus 唯讀 Metrics | allowlisted counters | 高基數／資訊外洩 | 無或 aggregate；Auth/network boundary |
| 簽章 OTA | signed artifact/compatibility | 韌體磚化、密鑰外洩 | firmware release tables；簽章驗證與人工回復 |
| Canary Rollout | rollout state/stop rule | 大量故障 | group/device assignment；自動停損 |
| 地點地圖 | coarse tiles/precision policy | 去匿名化 | privacy version；預設粗化與本機運算 |
| 人物群組 | local embeddings/consent | 生物辨識高敏感 | 專案另行隱私審查；預設關閉、可完整刪除 |

## 10. 第一個 Draft PR 的邊界

本 PR 只建立：單一 Metadata Schema、繁體中文搜尋／篩選／基本進階模式、Dirty tracking、
partial transaction、Snapshot/Diff/Rollback、來源與最終值、跨欄位驗證、Impact Preview、
安全 Import/Export、稽核、RBAC/CSRF、Migration 18 與測試。

不包含完整裝置群組、選片模擬、Batch lifecycle、拖拉版型、新通知通道、OTA、Prometheus、
人臉、地圖、大規模重分析、正式清理引擎、Shell／SQL／Python Console、Docker 或協議修改。
