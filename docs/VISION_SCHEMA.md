# Vision Schema：現行輸出契約

現行 `SCHEMA_VERSION=6`。權威實作：
`inktime/app/domain/analysis/schema.py` 的 `ANALYSIS_JSON_SCHEMA`、
`validate_model_response` 與 `validate_analysis_result`。

所有物件欄位必填且禁止額外欄位：

| 欄位 | 契約 |
|---|---|
| schema_version | 整數 6 |
| types | 1–3 個不重複分類，值見 ALLOWED_TYPES |
| memory_score / visual_score | 有限數值 0–100；一般回看價值／視覺吸引力 |
| special_level | 整數 0–4 |
| side_caption | 依照片寫成完整一句，1–20 字；預設只限制非空與最多 20 字，不要求固定長度 |
| content_filter | sexualized_content、explicit_nudity、female_glamour_portrait；各含 detected boolean 與 confidence 0–1 |
| display_exclusion | none、adult_media_cover、commercial_model_photo、screenshot、document_record 之一；含 confidence 0–1 |
| visual_orientation | rotation_cw（0/90/180/270 或 null）、confidence 0–1、ambiguous boolean、1–6 個不重複 evidence（ORIENTATION_EVIDENCE） |

方向為 EXIF transpose 後仍需的順時針旋轉。模型入口在完整 shape 驗證後
保守正規化：rotation_cw=null 時 ambiguous=true；出現
insufficient_visual_cues 時只保留該 evidence、rotation_cw=null、
ambiguous=true、confidence≤0.5。不捏造旋轉或提高信心。

v6 的顯示安全門檻固定為 0.60：`content_filter` 明確陽性，或
`display_exclusion` 非 `none` 且達門檻時自動排除；低信心疑似內容、
`none` 但低信心，以及無法確定的方向進入 `/review/photos`。待確認時
`eligible=0`，人工確認前不送相框；明確安全排除不可人工恢復。
正式發布與裝置下載都要求照片目前具有 v6 語意安全分析。

## 相容性與相關契約
新模型回應使用 v6；儲存結果驗證仍接受 v4/v5/v6，按版本驗證完整形狀。
不因本文件搬移而重分析、刪除舊 JSON 或改寫 DB 欄位。
[歷史 v4 契約](archive/contracts/VISION_SCHEMA_V4.md) 僅在舊資料／migration
問題需要時讀取，不指導新的模型回應。

排名、最愛、內容排除與發布資格見
[現行選片契約](analysis/PHOTO_SELECTION_AI_FIRST_ZH_TW.md)。
本文件描述資料契約；部署與真實模型品質須另有驗收證據。
