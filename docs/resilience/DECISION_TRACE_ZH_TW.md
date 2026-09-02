# 決策追蹤

Decision Trace 記錄「為何選片」，與記錄模型請求／回應的 [AI Trace](../guides/ACTIVITY_AI_TRACE_ZH_TW.md)不同。本機模式可有 Decision Trace 而完全沒有模型請求。

每次由 `RenderService.publish()` 自動選片而完成的正式發布都會建立 `selection_trace_id`。Trace 保存演算法版本、設定雜湊、版型、fit mode、Release 與最多 50 筆候選分數拆解；不保存原始路徑、完整 EXIF 或 Token。`GET /api/decision-traces` 使用 page/page_size（最大 100）分頁；詳細資料為 `GET /api/decision-traces/<trace_id>`。

Trace 寫入屬可觀測性資料：若其失敗，既有已成功的原子 Release 不會被回滾。可透過 `POST /api/decision-traces/<trace_id>/feedback` 提交回饋，需管理員登入與 CSRF。
