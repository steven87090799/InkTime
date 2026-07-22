# InkTime 錯誤碼

錯誤碼是穩定的程式介面；訊息可改善，但既有錯誤碼不得任意改名。管理介面、API、工作事件與結構化 Log 使用同一組代碼。

| 錯誤碼 | 說明 | 建議處理 |
|---|---|---|
| `AUTH-001` | 登入失敗 | 確認帳號密碼；連續失敗後等待 15 分鐘 |
| `AUTH-002` | CSRF 驗證失敗 | 重新整理頁面後再操作；勿重送過期表單 |
| `AUTH-003` | 尚未登入或 Session 過期 | 重新登入 |
| `AUTH-004` | 角色權限不足 | 使用 administrator 或改由管理員操作 |
| `PATH-001` | 路徑超出允許範圍 | 檢查相片庫、輸出目錄與請求路徑 |
| `DB-001` | 資料庫連線失敗 | 檢查 Volume 權限、磁碟與 SQLite Lock |
| `DB-002` | Migration 失敗 | 停止服務，保留備份並查看 Migration 原始錯誤 |
| `MIGRATION-002` | 偵測到未完成 Migration | 禁止啟動 Worker；停止三服務並由訊息指定的 pre-migration 備份離線還原 |
| `MIGRATION-003` | 資料庫 Schema 高於程式版本 | 切回相容映像或升級程式；不可用舊程式降級寫入 |
| `MIGRATION-004` | Schema 已提交但 Migration 歷史收尾失敗 | 保持服務停止並由升級前備份回復；不可把此狀態誤當成已 rollback |
| `SCAN-001` | 無法讀取照片 | 檢查 NAS 掛載、檔案權限或檔案是否仍存在 |
| `SCAN-IO-002` | 完整走訪或批次資料庫寫入有重大錯誤 | 不執行 Missing reconciliation；修復掛載／磁碟後重掃 |
| `SCAN-MISSING-THRESHOLD` | 預計 Missing 超過安全比例 | 保留掃描結果且不更新照片；確認掛載與數量後由管理員人工確認 |
| `SCAN-MISSING-004` | 欲確認的 Missing 結果已有較新掃描 | 只確認同一照片庫最新一次等待確認的掃描，避免舊候選覆寫新狀態 |
| `THUMB-001` | 單張縮圖建立失敗 | 原照片與掃描繼續保留；修復圖片／權限後重試 |
| `IMG-001` | 圖片解碼失敗 | 檢查格式與檔案完整性 |
| `IMG-002` | 字型缺失、損壞、格式不合法或缺少短文案字元 | 到「渲染」選取內建繁中字型，或上傳涵蓋所需字元的 TTF／OTF／TTC |
| `VLM-001` | Provider API 逾時 | 檢查端點、調高逾時或等待重試 |
| `VLM-002` | Provider Rate Limit | 依 Retry-After 等待或降低並行數 |
| `VLM-003` | 模型回傳無效 JSON | 工作會進行一次修復；仍失敗則進錯誤佇列 |
| `VLM-004` | 模型輸出不符合 Schema | 檢查模型 JSON Schema 能力與允許類型 |
| `VLM-005` | Provider 熔斷 | 等候冷卻或確認故障轉移 Provider |
| `BUDGET-001` | 工作預算超限 | 工作已暫停，不會再送出新請求 |
| `BUDGET-002` | 每日或每月預算超限 | 調整預算或等候下一週期 |
| `JOB-001` | 工作狀態轉換不合法 | 重新整理工作狀態後再操作 |
| `JOB-002` | Worker 租約逾時 | 系統會安全地回收項目並重試 |
| `RENDER-001` | 渲染失敗 | 檢查來源照片、字型、版型與輸出權限 |
| `RENDER-002` | 發布校驗失敗 | 不會更新 latest；舊版本仍可使用 |
| `RENDER-003` | 顯示 Profile 不支援 | 從 Web 選擇內建四色／GDEP 六色／GDEY 七色 Profile |
| `RENDER-004` | 抖動、色差或強度不合法 | 檢查 Web 渲染設定範圍 |
| `DEVICE-001` | 裝置驗證失敗 | 確認 Bearer Token、裝置啟用狀態或重新配對 |
| `DEVICE-002` | 發布檔案校驗失敗 | 裝置應保留舊畫面並回報失敗 |
| `DEVICE-CONFIG-PROFILE` | 設定版本倒退或面板 Profile 不相容 | 核對面板、韌體 compile flag 與 Web 裝置設定 |
| `DEVICE-OFFLINE` | 裝置超過門檻未連線 | 檢查電源、Wi-Fi、Token、刷新週期與 N100 可達性 |
| `NOTIFY-WEBHOOK` | Webhook 暫時或永久失敗 | 查看裝置頁嘗試次數、HTTP 狀態與端點 Log |
| `BACKUP-001` | 備份建立或驗證失敗 | 檢查空間、權限與資料庫完整性 |
| `BACKUP-002`／`BACKUP-003` | 備份格式或 SHA-256 損壞 | 不會覆蓋現有資料庫；改用另一份已驗證備份 |
| `RESTORE-001` | 仍有 InkTime 程序持有資料庫 | 停止 Web、Worker、Scheduler 後再執行離線還原 |
| `RESTORE-002`～`RESTORE-006` | 還原內容、Schema 或驗收失敗 | 工具會自動保持／回復原資料庫；保留安全副本並查看明確錯誤 |

API 錯誤回應至少包含 `error_code` 與繁體中文 `message`；內部例外堆疊只寫入受保護的記錄，不傳送給 viewer 或裝置。
