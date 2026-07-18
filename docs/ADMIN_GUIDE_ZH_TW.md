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
| `render.font_path` | 空 | 有效 TTF/OTF/TTC | 缺字會停止發布 | 否 |
| `render.profile` | safe_4c | 四色／GDEP 六色／GDEY 七色 | 必須與裝置面板相符 | 否 |
| `render.dither` | floyd_steinberg | none／Floyd／Atkinson／Bayer 4／8 | 誤差擴散發布 CPU 較高 | 否 |
| `render.dither_strength` | 1 | 0–2 | 過高會增加色點 | 否 |
| `render.color_distance` | oklab | oklab／rgb | 切換會改變色彩映射 | 否 |
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

## ESP32 遠端設定

首次 AP 配對只填 Wi-Fi、InkTime URL 與一次性 Token。之後從「裝置」編輯每台 ESP32 的名稱、啟停、面板 Profile、IANA 時區、每日 `HH:MM` 與 0°／180°；下一次取得 Manifest 自動套用。裝置頁以期望版本／ACK 區分「已儲存」與「裝置已生效」，並顯示離線狀態、通知、firmware、RSSI、free heap／PSRAM、下載計數與最後錯誤。完整協定、抖動與通知見[裝置可靠性與六／七色渲染指南](DEVICE_COLOR_NOTIFICATION_GUIDE_ZH_TW.md)。

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
