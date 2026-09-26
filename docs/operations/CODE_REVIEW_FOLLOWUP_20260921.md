# Code review remediation 後續修復與歸屬

基底為同一分支 `fix/code-review-remediation` 的 `cb5a8a2`，保留原作者的 `6e7225a`、`cb5a8a2`；以下修改在其上追加 commit，沒有重寫原提交。目標是補齊本次差異涉及的升級、安全對帳與故障恢復契約。

## 這次補修的部分

1. **預算回收必須有證據。** 移除 `snapshot()` 的 24 小時自動排除。排程只回收已證實未送出，或已保存回應／完成且具有確定費用帳目的 Vision 保留額；明確零費用也是有效證據。未對帳 Vision、Batch、診斷與無法辨識的保留額持續計入。Batch 由既有 importer 在完整對帳後釋放。原因：時間經過不能證明遠端沒有收費，也不能讓其他工作重新花掉同一筆預算。
2. **備份改成 `VACUUM INTO` 指定檔案。** 在備份磁碟上的私有暫存目錄產生壓縮副本，驗證後沿用安全 fd、fsync 與 ZIP 發布流程。原因：`temp_store_directory` 是程序全域設定，不能在 web 程序中改到稍後會刪除的目錄。參考 [SQLite VACUUM INTO](https://www.sqlite.org/lang_vacuum.html)。
3. **Provider 保留可驗證的舊快取映射。** 同時識別目前與 Migration 61 時期的語意雜湊；只有目前設定仍符合保存的雜湊才接受舊 analysis_revision。下次非語意設定儲存時自然轉換為新對應。原因：原修改可能讓曾調整 timeout 等設定的使用者在升級後失去已付費快取，或讓 frozen Job 被拒絕。端點、模型等真正影響請求的變更仍使映射失效。
4. **JSON enum 定點修復及 Migration 63。** 移除尚未合併之 Migration 62 的整段字串 replace，改由 63 解析 JSON，只修 `types` 及已知分析結果包裝。補齊照片 `raw_json`／`semantic_json`、快取、Job、Trace；caption、其他自然語句和保留的原始供應商回應不修改。原因：只修 types_json 不足以恢復實際使用 raw_json 的繼承流程；整段替換會誤改文字。已套用早期 62 的資料庫也會執行 63；已被舊程式改掉且無原始證據的文字不猜測還原。
5. **soft timeout 當下建立 shutdown deadline。** 使用既有 `request_stop()`，保留無法安全終止執行緒的 ambiguous 結果與禁止自動重試。原因：原新增 deadline 位於迴圈之後，卡住的 future 使該段永遠無法到達。
6. **Shadow 恢復誠實的觀察模式。** 不把沒有 cleanup handler 的政策標為自動清理。Migration 63 恢復早期分支未自訂的預設值，管理員自訂值不改。其餘五種已有實作的清理政策保留，補充升級後會自動刪除的說明與實際過期／未過期資料測試。原因：只測 dry_run 旗標不能證明有刪除。
7. **補齊錯誤說明。** 登錄 JOB-005、JOB-SHUTDOWN-CANCELLED、SESSION-003，明確區分本機工作重試、未知付費請求對帳與密鑰復原。原因：缺漏已使原 head 的 Hosted error-catalog contract 及 Repository gate 失敗。
8. **補強回歸案例與修正交付說明。** 涵蓋超齡未知／已對帳／明確零費用、舊 Provider alias 與 frozen route、61/62 升級及重複 migration、原始回應不被修改、Shadow 管理員政策、連續備份與其他 SQLite connection、沒有外部 stop 的 Worker timeout。原報告改標歷史資料，不能以它的 Fixed 標籤或缺少明細的總數作驗收依據。

## 原作者修好、保留的部分

- 保護 OpenCC 轉換中的協定 enum，避免「文件」被改成「檔案」後校驗失敗。
- 排除 Provider priority／supports_batch 對語意 revision 的影響；本次補上舊版本映射相容性。
- API 手動掃描設定明確的執行期限，與排程掃描規模對齊。
- 正常關機中斷 isolated local job 時使用可重試分類，避免直接 dead-letter；不擴大為未知付費請求自動重送。
- Worker 主迴圈記錄未預期例外並有界等待後恢復；不宣稱所有 poison-job 情境因此消失。
- 主密鑰 fingerprint 可偵測已有 fingerprint 的 DB 與密鑰不符；不宣稱首次升級或舊備份已有可驗證的密鑰身分。
- 還原先驗 manifest，若有 forward migration，之後不再拿遷移前筆數誤判結果；完整性檢查保留。
- pair selection 去重並保留原本仍應入選的不同照片。
- scheduler 的 mark_enqueued 例外限制在單一排程，不阻斷後面的排程。
- NVS 離線重試計數改用合法短鍵 `offretry_try`。
- 絕對時間 deep sleep 增加 24 小時上限與負值保護。
- persistence fixture 使用相對當下時間，避免固定日期失去代表性；本次另測超齡未知費用仍被保留。
- 已有實作的五種預設資料清理保留，但明確記錄其自動刪除行為與管理員設定保護。

## 驗證與界線

本地只做 Python 語法、差異與靜態契約核對，依 AGENTS.md 不執行本地 pytest、Docker、Provider 呼叫或韌體編譯。回歸測試由推送後的 Hosted CI 執行；新增測試不等於已通過，最新 head 的執行結果以 GitHub 為準。

原 head `cb5a8a2` 的 Hosted run `35600144804` 有 1975 passed、1 failed（錯誤碼目錄），且 Repository gate 失敗；該結果不可當成後续 commit 的通過證明。原 head 的 ESP32 compile 已成功，但本次沒有刷機或真機睡眠／喚醒驗收，也沒有部署或修改使用者資料庫。

未知費用仍須對帳，Shadow 真正的清理 handler 仍未實作；本次移除的是不實的自動清理承諾。這些限制不應以刪掉保留額或只修改旗標掩蓋。原報告所稱 42 個其餘問題不在本次逐項重審範圍。
