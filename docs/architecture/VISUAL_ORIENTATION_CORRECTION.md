# 照片視覺方向校正

`rotation_cw` 的定義是原圖先執行 `ImageOps.exif_transpose()` 後，畫面仍需額外順時針旋轉的角度。EXIF 只在這個固定步驟套用一次；AI 與人工設定絕不重新解讀 EXIF。

中央 `resolve_effective_orientation` 的優先順序為人工設定、合格 AI、EXIF 已正規化、無額外旋轉。90/270 度需 confidence 0.95、180 度需 0.98；`ambiguous=true` 或 null 只顯示建議。單圖、photo_pair、adaptive_memory、Preview、Test Release 與正式 Release 都經 `_load_oriented_photo`，在 Crop/Contain/Cover 前完成方向校正。

Schema v1 可缺少 `visual_orientation`，runtime 會安全補 unknown；Schema v2 必須包含完整欄位，新 Provider 和 cache key 均使用 v2。Migration 19 保存原始 EXIF、AI 結果與人工設定。新照片在本機掃描就保存 EXIF；同路徑 SHA 改變時會清除所有舊 AI、人工、裁切與本機特徵，重新寫入新 EXIF。內容未變不清除人工設定。Exact duplicate 可繼承 AI 方向結果，不能繼承人工設定。

人工設定會建立 Photo Event，不改原圖或 EXIF、不呼叫 AI、也不自動發布。Release Manifest 以相容的 `photo_orientations` 附加每張照片的 rotation/source/confidence/exif_normalized，既有 ESP32 payload 不變。目前沒有跨請求 Renderer Image/Preview cache；未來若加入，fingerprint 必須包含內容 SHA、有效方向、來源、人工修改時間、renderer 版本、版型、fit、crop 與 panel profile。舊照片不會批次重新分析；沒有可靠視覺線索的影像仍需人工決定方向。
