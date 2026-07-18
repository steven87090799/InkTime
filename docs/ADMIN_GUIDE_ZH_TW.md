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
| `analysis.concurrency` | 2 | 1–32，NAS 建議 2–4 | 過高觸發限流／記憶體 | 是 |
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
| `device.legacy_api_enabled` | false | 僅遷移期 | URL 金鑰不安全 | 是 |
| `system.log_format` | human | human/json | 集中 Log 建議 json | 是 |
| `security.session_minutes` | 30 | 5–1440 | 過長增加共用裝置風險 | 是 |
| `backup.schedule_enabled` | true | true/false | 關閉後需手動備份 | 否 |
| `backup.hour` | 3 | 0–23 | 避開大量分析 | 否 |
| `backup.retention` | 14 | 1–365 | 過低縮短回復期 | 否 |

所有修改寫入 `setting_history`；Secret 永不寫入摘要。修改需重啟欄位後，使用 `docker compose restart`。

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
