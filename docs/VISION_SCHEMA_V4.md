# Vision Schema v4：內容排除與排序

本文件描述 Schema v4 與 `ranking-v5-ai-first` 選片契約（2026-09-05 更新；部署狀態另見交付紀錄）。

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

模型不能輸出最終排除決定。Server 依目前設定、分類與門檻，在保存分析的同一交易套用 `eligible=0`、`auto_excluded` 和獨立 reject_reason；selection_score 為零，正式選片排除。三種分類各自包含 `detected` 與 `confidence`，可以同時成立；Server 各自套用開關與門檻，任一命中即排除。未偵測到或信心不足的分類不觸發排除，關閉其中一類不會遮蔽其他類。全部命中原因保存在 reject details，primary reason 依 sexualized、nudity、glamour 的固定順序選取。Screenshot 仍由本機辨識。

Prompt 明確禁止把單人女性、普通旅遊照、自拍、生活、家庭、工作、畢業、活動、自然抓拍或運動直接判為女性寫真，也不推論真實性別身份。這是模型分類規範，不是對任何模型準確率的保證。

Favorite 只調整 Vision 排名，不繞過 AI 內容排除，包含重用分析。Manual Restore（人工恢復）才可覆寫內容排除；普通重掃、重新分析及重用結果不覆寫人工決定，只有明確重新套用自動規則才重新評估儲存的分類。本機品質規則原有的最愛保護不影響這項 AI 內容規範。設定開關不會追溯刪除歷史判斷；使用重新套用或人工恢復處理既有排除。

## 排序

本機品質是資格門檻，模型分數決定自動選片順序。須同時具備 `local_features_status=complete`、有效 Schema v4 semantic 結果、非空 ranking_score、合格且存在的原檔，才可進入 automatic_ai 候選與正式發布。本機／AI 任一未完成時等待，不以 local-only 分數補位；明確的 local_only 等模式保留。

`base = memory * 0.67 + visual * 0.33`

`effective_special_level = clamp(ai_special_level + favorite_adjustment, 0, 4)`

`AI 選片分 = clamp(base + special_bonus, 0, 100)`

Special bonus 固定為 `0 / 2 / 5 / 9 / 14`，最愛提升 1 級。本機品質分不加權，不再估計照片庫稀有度，不換算 percentile，也不加入 E6。E6 只供顯示效果參考，並非經驗證的清晰度門檻。`render.memory_threshold`、`render.e6_weight` 僅保留設定相容性，已不影響自動選片。

本機沿用保守且共用的 `evaluate_local_quality()`：嚴重模糊、極端曝光且低對比、解析度過低等明確問題可排除；疑似品質問題不扣 AI 分。本機候選品質分仍保存以利診斷與本機模式使用，不能解讀為照片的回看價值。人工恢復與既有最愛保護保留。

`top_ranked` 依 AI 選片分排序，不再加入日期或播放次數加減分；多樣性只用於 AI 同分時的選擇。`history_today` 仍依明確設定先選同日、鄰近日及一般候選，各範圍內由 AI 排序。自動 AI 分析的有界佇列先處理尚無有效 AI 結果且本機已完成的照片；保留原本數量、預算與上傳限制。

## 儲存與輸出

Migration 54 完整保留已部署 main 的本機／語意來源分離契約，不改寫 SQL 或重複新增 `score_kind`。Migration 55 新增 v4 分析與權重欄位，56 持久化照片庫排名重算狀態，57 將 v1–v3 semantic 標記為 legacy 並解除舊 E6 自動排除。歷史原始 JSON 與分數保留，需重新分析才進 v4 排名。

Migration 57 只恢復 `auto_excluded`、`manual_override=0`、`reject_rule=local-quality` 且理由恰為 `e6_below_threshold` 的照片；保留其他自動排除與全部人工決定，並寫入轉換事件保存舊證據。`FEATURE_VERSION=local-quality-v6` 使未變動照片在下次本機特徵掃描重新計算，品質規則另以 `local-quality-policy-v2` 標示；E6 僅供參考。

歷史 scoring profile 保留 beauty／technical／emotion 原欄位和值，新增 visual／local 欄位在舊列為 NULL，並以 `ranking_contract_version` 區分舊版與 v4。v4 新列的舊 beauty／technical 欄位只用於滿足原 SQLite 約束，不供 v4 排名或 UI 讀取。舊版 profile 僅供查閱，不能直接還原為 v4；v4 還原仍建立新版本。原圖與人工決定不會因 migration 刪除。

Migration 58 以固定公式重算既有有效 v4 分析的衍生排名與版本標記，不呼叫模型、不改原始 JSON、短句或人工排除。舊評分版本完整保留，啟動後由既有設定建立新權重版本。v1–v3 仍需重新分析。

Caption 預設 10／60／100 字，side_caption 8–16 字；全分析上限 1200 tokens（含最大 100 字描述、16 字短句、有限陣列、JSON 鍵與格式餘裕）。短句預設為有文氣、輕微幽默的相框旁白（幽默 2／5、詩意 3／5），從可見細節做小反差，避免照片摘要、形容詞清單與半截比喻；提示詞提供兩個語感範例並禁止照抄。提示詞版本更新，不會改寫既有短句；使用者明確修改的風格設定保留。

評分 rubric 只送一次；自訂內容取代預設參考，固定輸出與評分邊界獨立保留。

## 提示詞與評分控制中心

從「AI 分析 → 提示詞與評分」或設定頁進入 `/scoring`。

- **已鎖定**：全部 Schema v4 必填欄位、型別、範圍、內容分類及方向判斷；可展開查看程式產生的欄位規格與固定提示詞原文。
- **可調整**：`analysis.scoring_rules` 的評分參考、各分數的高低分條件、參考權重與 special_level 情境門檻。允許 1～12,000 字元，可精簡文字；不改變伺服器 67／33／0 合成公式與 Special bonus。
- **預覽**：使用與 Vision 傳輸相同的組裝函式，顯示 System、User、Provider 的 response_format，以及僅在 JSON 驗證失敗時使用的修復指令。預覽不呼叫模型、不儲存版本。字元差異不等於 Token 節省，實際 Token 以服務商用量為準。
- **適用範圍**：新建照片分析工作使用目前進階文案設定；現有單張測試台未套用該設定，預覽可切換用途。已建立工作沿用建立時的規則及文案快照，實際請求可從 `/ai/traces` 查閱。
- **版本與權限**：管理員儲存及還原都建立新版本，沿用設定快照與稽核；既有照片不會自動重新評分。查看者只能讀取。API 僅接受名稱及規則，傳入 Schema 或固定權重會拒絕。

目前設定可透過 `GET /api/v1/scoring/prompt` 讀取；管理員以 `POST /api/v1/scoring/prompt/preview` 傳入 `{"rules":"..."}` 預覽。可用 `scope=analysis`（預設）或 `scope=scoring_test`，兩種模式都不送出付費請求。

## 驗證範圍

回歸測試涵蓋已部署 main schema 54 → 57、重複啟動、歷史權重與分析保留、E6 精準恢復、三類重疊與獨立門檻、Favorite／人工恢復，以及 review 的分類最低信心。

依 `AGENTS.md`，測試由 Hosted CI 執行；本機僅作靜態檢查。測試使用小型合成資料與 fake Provider；不代表 NAS 升級實測、真實模型分類準確率或大型照片庫效能驗證。

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
