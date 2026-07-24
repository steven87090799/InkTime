# 照片視覺方向校正

InkTime 一律先讀取原圖並套用 `ImageOps.exif_transpose()`；AI 的 `rotation_cw` 是此正規化畫面仍需順時針轉動的角度，因此 EXIF 不會重複套用。分析縮圖與既有照片分析使用同一個模型請求，Schema v1 的新增 `visual_orientation` 欄位會保存 rotation、confidence、ambiguity 與受限 evidence。

中央 `resolve_effective_orientation` 的優先順序為人工設定、非模糊且達門檻的 AI、已正規化 EXIF、原始方向。90/270 度需 0.95，180 度需 0.98；`ambiguous` 或 null 只顯示建議、不自動轉。Renderer 在 crop/contain/cover 前套用有效額外旋轉，且人工修正只寫 Photo Event，從不改寫原圖、EXIF、AI 結果或自動發布 Release。

Migration 19 僅新增相容欄位；歷史照片方向為 unknown，不會批次重新分析。未來正常分析或使用者明確要求分析才會取得結果。快取以新版 prompt 指紋隔離，舊快取仍可讀取並以 unknown 方向安全退化。已知限制是沒有視覺線索的食物俯拍等影像不能可靠自動校正，必須人工設定。
