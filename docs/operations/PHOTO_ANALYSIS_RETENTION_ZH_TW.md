# Photo Analysis 歷史資料保留與安全清理

## 保留契約

`photo_analysis` 清理只處理已證明不再具有目前狀態、人工稽核或繼承用途的歷史列。每張照片固定保留：

- 依 `created_at DESC, id DESC` 排序的最新分析。
- 符合目前 Analysis Plan fingerprint 或完整 spec 的最新分析。
- 所有被 `photo_reviews`、`photo_review_events` 指向的分析。
- 所有被 `semantic_json.inherited_from.analysis_id` 指向的來源分析。
- 除上述項目外，最近 2 筆未引用歷史分析。

若同一張照片存在不合法的 `semantic_json`，因為無法證明其中沒有邏輯引用，該照片的全部分析都會 fail closed 保留。規則不另加年齡門檻；安全邊界由預設 dry-run、明確 confirmation、依賴重查與小批次 transaction 提供。

## 操作契約

管理員使用 `POST /api/v1/maintenance/photo-analysis-retention`：

- 空 JSON 或 `{"dry_run": true}` 只回傳 deterministic inventory，不刪資料。
- inventory 包含各保留類別、候選總數、候選照片數、時間範圍與最多 50 筆候選 sample。
- 實際刪除必須同時傳入 `dry_run=false`、`confirmation=DELETE_UNREFERENCED_PHOTO_ANALYSIS`，以及上一個 dry-run 回傳的 `expected_inventory_digest`。
- `batch_size` 預設 200，合法範圍 1–500；一次請求只提交一批。
- 每批使用 `BEGIN IMMEDIATE`，刪除 SQL 會在同一個 write transaction 內重新計算 current/latest/reference/buffer 條件。
- 若 current plan 或候選集合在 dry-run 後改變，digest 不一致會回傳 `409 RETENTION-003`，不刪任何資料；必須重新 dry-run。
- 不執行 `VACUUM`。若仍有候選列，管理員必須重新查看回應後再明確送出下一批。

實際執行前應先保存當下資料庫備份，並保留 dry-run inventory 作為變更證據。
