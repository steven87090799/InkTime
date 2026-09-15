# 照片選片：現行設計契約

## 資格先於排名
本機品質是 quality gate，不是 AI 排名權重。automatic_ai 候選須本機特徵完成、
具有有效 semantic 分析（現行 v5 或相容 v4）、非空 ranking_score、合格且存在
的原檔；本機／AI 未完成就等待，不以本機分數補入 AI 候選。明確選擇的
local_only 模式保留。候選與正式發布須維持相同資格邊界。

來源：`inktime/app/repositories/render_candidates.py` 的
`SEMANTIC_SQL_PREDICATE`；發布追 `inktime/app/services/display_prepare.py`。
品質細節按需查 `evaluate_local_quality`，不要為排名載入掃描歷史。

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

`side_caption` 是 8–16 字的完整相框短句，以可見細節為依據；
`visual_orientation` 是 EXIF transpose 後仍需的順時針旋轉判斷。
不確定時保守處理，不捏造方向或提高信心。完整現行欄位與相容性見
[Vision Schema](../VISION_SCHEMA.md)。

[歷史設計與截圖算式](../archive/analysis/PHOTO_SELECTION_AI_FIRST_20260905.md)
僅供回歸追查，不代表目前部署或實板驗收。
