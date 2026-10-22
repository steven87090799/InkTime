# 照片選片：現行設計契約

## 資格先於排名
本機品質是 quality gate，不是 AI 排名權重。automatic_ai 候選須本機特徵完成、
具有現行 v6 semantic 安全分析、非空 ranking_score、合格且存在的原檔；
本機／AI 未完成就等待，不以本機分數補入 AI 候選。v4/v5 分析仍可閱讀，
所有執行模式的正式發布均須通過 v6 安全檢查。候選與正式發布維持相同資格邊界。

來源：`inktime/app/repositories/render_candidates.py` 的
`SEMANTIC_SQL_PREDICATE`；發布追 `inktime/app/services/display_prepare.py`。
品質細節按需查 `evaluate_local_quality`，不要為排名載入掃描歷史。

## 避免反覆顯示
Migration 72 保存每張照片的 `display_count`／`last_displayed_at` 與
`selection_count`／`last_selected_at`。只有 `DISPLAY_COMPLETED` 成功回報
會增加顯示次數；下載、渲染與發布均不代表實際顯示。雙照片版面兩張各計一次，
相同 Queue Item 的重複 ACK 不重計，不同裝置的成功顯示分別計次。
既有成功 ACK 回填；既有發布紀錄保留為未確認顯示，不虛增顯示次數。

自動選片先比較 `max(display_count, selection_count)`，在允許的日期候補範圍
內優先未使用的合格照片。需要重用時優先次數較少者，再依日期階段與距離、
較久未使用、原始 AI 分數與多樣性選擇。`history_today_fallback=none` 仍不
跨出同日範圍。配對先嘗試未使用來源，仍須符合方向與非重複內容檢查。
系統選定次數與成功發布在同一交易保存，讓沒有顯示 ACK 的舊裝置也能輪替；
未發布成功不增加選定次數。此規則不改寫模型的原始 AI 分數。
同次發布的橫／直向候選池重疊時，每張來源只增加一次選定次數；
配圖階段加入的第二張來源也納入歷史。預覽配圖不寫入正式選片紀錄。
`seasonal_ai` 僅在同期日期窗口內套用輪替，不套用窗口外的日期候補。

Web「墨水屏 → 選片與顯示歷史」(`/display-history`) 可按日期／照片 ID 查詢。
每次自動選片保存完整的實際候選池（依各階段上限，合計最多 3000 張），
包括未選中的照片、分數、拍攝日期、當時次數、選定與發布結果。
候選分頁展示，不受舊 Decision Trace 前 50 名或 60 天清理限制；納入 DB 備份。
升級前未保存的候選無法回溯重建。手動指定仍按操作者的選擇發布。

## 分析失敗通知
Migration 73 在失敗狀態的同一 DB 交易寫入通知：一般模型失敗、逾時、
請求結果未確認，以及沒有 AI Trace 的 Batch 項目／分析工作失敗均涵蓋。
通知保留至使用者標記已讀，各頁顯示未讀警示，開啟頁面每 30 秒更新。
`/notifications` 連到照片、工作與實際 AI Trace；通知不會重送付費模型請求。
已啟用 Webhook 且已設定網址時，新通知沿用既有 outbox、去重與傳送重試。
未啟用 Webhook 仍有站內警示；未設定外部通知時，使用者需開啟平台查看。
通知只帶識別碼與錯誤代碼，不包含模型請求／回應或 Provider 憑證。
同期任務停止、無模型結果及舊付費請求結果不明等沒有 Trace 的失敗也會通知；
副本沿用失敗不會覆寫原內容已完成的分析結果。

## AI 分數與最愛
```text
base = round(memory_score × 0.67 + visual_score × 0.33, 2)
effective_special_level = min(4, special_level + (1 if favorite else 0))
bonus = [0, 2, 5, 9, 14][effective_special_level]
ranking_score = round(clamp(base + bonus, 0, 100), 2)
```
`memory_score` 是一般回看價值，不能推測私人關係或對使用者的重要性；
`visual_score` 是視覺吸引力；`special_level` 0–4 表示特殊事件／瞬間。
最愛提升一級，上限 4；本機品質、照片庫稀有度、百分位與 E6 不加進此公式。
來源：`inktime/app/domain/analysis/scoring.py` 的 `ranking_components`。

## 內容、短句與方向
內容分類由 server 依開關、detected 與 confidence 門檻決定是否排除，
不作排名加分。最愛不繞過 AI 內容排除；人工恢復可覆寫，普通重掃／重新分析
保留人工決定。分類門檻來源：`inktime/app/domain/analysis/content_filter.py`。

`side_caption` 是依照片產生、不超過 20 字的完整相框短句，以可見細節寫畫外之意，不逐項描述畫面；
`visual_orientation` 是 EXIF transpose 後仍需的順時針旋轉判斷。
不確定時保守處理，不捏造方向或提高信心。完整現行欄位與相容性見
[Vision Schema](../VISION_SCHEMA.md)。

[歷史設計與截圖算式](../archive/analysis/PHOTO_SELECTION_AI_FIRST_20260905.md)
僅供回歸追查，不代表目前部署或實板驗收。
