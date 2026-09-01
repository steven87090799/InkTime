# Vision Schema v4：內容排除與排序

來源基準：GitHub `steven87090799/InkTime` main `a5f935568c3693d90bb78432acfdf79913c59445`。

模型只輸出 Schema v4：兩個 0–100 分數（memory、visual）、special_level（0–4）、最多兩個 special_codes、types、people_count、caption、side_caption、content_filter、subject_position、text_safe_area、visual_orientation。每個物件禁止額外欄位；不接受 v1/v2/v3 或 Grade 正規化。

每次 Vision（包括同步、Batch、快取、重用分析）都必須保留方向與內容分類。方向是 EXIF transpose 後還需順時針旋轉的角度；null 必須 ambiguous=true，只有 insufficient_visual_cues 時信心不得大於 0.5。無效模型 JSON 走既有一次文字修復流程，不增加 Vision 呼叫。

## Server 內容排除

| 設定 | 預設 |
| --- | --- |
| `analysis.exclude_sexualized_content` | true |
| `analysis.exclude_explicit_nudity` | true |
| `analysis.exclude_female_glamour_portraits` | true |
| `analysis.content_filter_min_confidence` | 0.85 |
| `analysis.female_glamour_min_confidence` | 0.90 |

模型不能輸出最終排除決定。Server 依目前設定、分類與門檻，在保存分析的同一交易套用 `eligible=0`、`auto_excluded` 和獨立 reject_reason；selection_score 為零，正常 percentile 母體與正式選片均排除。分類為 none/uncertain 不自動排除。Screenshot 仍由本機辨識。

Prompt 明確禁止把單人女性、普通旅遊照、自拍、生活、家庭、工作、畢業、活動、自然抓拍或運動直接判為女性寫真，也不推論真實性別身份。這是模型分類規範，不是對任何模型準確率的保證。

人工恢復與最愛受保護，普通重掃、重新分析及重用結果不覆寫；只有明確重新套用自動規則才重新評估儲存的分類。設定開關不會追溯刪除歷史判斷；使用重新套用或人工恢復處理既有排除。

## 排序

`base = memory * 0.50 + visual * 0.25 + local_quality * 0.25`

本機品質重用 `evaluate_local_quality()` 與 `local_candidate_score()`；E6 不計入此數值。

`effective_special_level = clamp(ai_special_level + rarity_adjustment + favorite_adjustment, 0, 4)`

Special bonus 固定為 `0 / 2 / 5 / 9 / 14`。最愛提升 1 級。本機 rarity 使用同照片庫合格 Vision 分析的 types、special_codes、people_count 區間（0、1、2–5、6–15、16+）。至少 20 張其他照片且某特徵在其他照片占比 ≤5% 才提升 1 級；最多加 1。聚合計數與分批重算讓分析先後順序不改變同一照片庫結果，只寫入有變化的分數。

`raw = clamp(base + special_bonus, 0, 100)` → 照片庫 percentile → distinguishing score → `display = distinguishing * (1-e6_weight) + e6 * e6_weight`。E6 預設 20%；本機照片沒有 AI 分數時沿用本機候選分。

## 儲存與輸出

Migration 54 加入 v4 儲存欄位，Migration 55 持久化照片庫排名重算狀態，Migration 56 加入 `score_kind` 並分離 semantic 與 local quality。舊資料僅作歷史稽核，不正規化、不進新排名，需重新分析。原圖與人工決定不會因 migration 刪除。既有 SQLite 歷史欄位保留，新分析不填 AI technical/emotion/reason；歷史 scoring profile 的舊 SQL 約束以固定零維持，不參與新公式。

Caption 預設 30／60／100 字，side_caption 8–16 字；全分析上限 1200 tokens（含最大 100 字描述、16 字短句、有限陣列、JSON 鍵與格式餘裕）。預設 rubric 只送一次，管理員確實自訂時才追加規則。

## 本機驗證範圍

Targeted suites：`test_analysis_schema`、`test_v4_ranking_content`、`test_scoring`、`test_scoring_rules`、`test_quality_policy`、`test_visual_orientation`、`test_vision_v4`。

只使用本機資料庫、小型合成照片與 fake Provider；不呼叫真實模型、不啟動 VM、不執行大庫 benchmark、不等待 Hosted CI。內容語義誤判率與大型照片庫聚合耗時未由這些測試證明。

## 變更檔案

- `benchmarks/golden/manifest.schema.json`
- `docs/VISION_SCHEMA_V4.md`
- `inktime/app/api/photos.py`
- `inktime/app/api/review.py`
- `inktime/app/api/scoring.py`
- `inktime/app/api/settings.py`
- `inktime/app/db/migrations.py`
- `inktime/app/domain/analysis/content_filter.py`
- `inktime/app/domain/analysis/plan.py`
- `inktime/app/domain/analysis/schema.py`
- `inktime/app/domain/analysis/scoring.py`
- `inktime/app/providers/openai_compatible.py`
- `inktime/app/repositories/photo_analysis_retention.py`
- `inktime/app/repositories/photos.py`
- `inktime/app/repositories/render_candidates.py`
- `inktime/app/repositories/reviews.py`
- `inktime/app/repositories/scoring.py`
- `inktime/app/repositories/settings.py`
- `inktime/app/services/analysis.py`
- `inktime/app/services/batch_analysis.py`
- `inktime/app/services/benchmark_metrics.py`
- `inktime/app/services/model_benchmark.py`
- `inktime/app/services/rendering.py`
- `inktime/app/services/scoring_lab.py`
- `inktime/app/web/error_messages.py`
- `inktime/app/web/templates/photo_detail.html`
- `inktime/app/web/templates/rendering.html`
- `inktime/app/web/templates/review_photos.html`
- `inktime/app/web/templates/scoring.html`
- `inktime/app/workers/runner.py`
- `scripts/ci/nas_update_e2e.sh`
- `tests/integration/test_vision_v4.py`
- `tests/unit/test_analysis_schema.py`
- `tests/unit/test_scoring.py`
- `tests/unit/test_scoring_rules.py`
- `tests/unit/test_v4_ranking_content.py`
- `tests/unit/test_visual_orientation.py`
