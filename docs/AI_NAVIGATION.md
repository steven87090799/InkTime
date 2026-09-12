# AI 修改導航：先定位，再讀取

本頁是修改專案的最小入口，不是完整產品手冊。先遵守根目錄 [AGENTS.md](../AGENTS.md)，並以[機器可讀任務索引](AI_CONTEXT_INDEX.json)核對入口與讀取邊界；只讀本頁與符合任務的那一列，再追必要的呼叫端、契約與測試。

## 每次任務的閱讀順序

1. 在 Git 根目錄執行 `git status --short`、`git log -1 --oneline`；保留既有修改。不要從父目錄搜尋其他 worktree、部署副本或 runtime data。
2. 選下表一列，並核對 `AI_CONTEXT_INDEX.json` 的同一個 `id`；先執行 `python scripts/ci/ai_context.py <task-id>` 或 `rg -n '符號或錯誤碼' 指定路徑`，再以 `sed -n '起始,結束p' 檔案` 讀取命中函式與上下文。找檔名用 `rg --files 指定目錄`。
3. 第一輪上限為 4 個檔案、800 行。證據不足才擴大至直接呼叫端、共用契約及相應測試，並記下擴大的原因；跨模組問題必須追完依賴，不可為省 Token 漏掉安全檢查。
4. 大文件先 `rg -n '^#{1,3} ' 文件` 找標題，再讀對應區段。不要把整份 README、USER_MANUAL.html、docs、tests、logs 或大型 JSON 載入對話；敏感、runtime、generated、archive 與二進位路徑依 `AI_CONTEXT_INDEX.json` 預設排除，除非任務指定精確路徑。
5. 修改後依 AGENTS.md 做本機靜態檢查與 `git diff --check`；測試、建置、韌體與 runtime 由 Hosted CI 負責。報告修改範圍、證據與未驗證項目。
6. 同一任務沿用已讀到的證據；換任務時交接「目標、檔案、符號、已確認事實、待辦」，不要複製全部工具輸出。檔案改變時才重新讀相關區段。

## 問題 → 必讀入口

以下程式路徑相對 Git 根目錄；先讀第一個入口，再按實際呼叫關係展開。測試用 `rg --files tests | rg '相關模組關鍵字'` 定位，只讀相關案例。

| task id／問題／區塊用途 | 程式入口 | 需要契約細節時讀 |
|---|---|---|
| `startup`／啟動、ready、三程序組裝 | `inktime/app/factory.py`、`inktime/app/bootstrap.py` | `docs/architecture/APPLICATION_FACTORY.md` |
| `scanner_photos`／掃描、EXIF、重複圖、本機品質 | `inktime/app/workers/scanner.py`、`inktime/app/domain/photos/preprocessing.py` | `docs/guides/LOCAL_ONLY_SELECTION_ZH_TW.md` |
| `analysis_scoring`／AI 評分、67/33、Schema、文案 | `inktime/app/domain/analysis/scoring.py`、`plan.py`、`schema.py`（同目錄） | `docs/VISION_SCHEMA_V4.md`、`docs/analysis/PHOTO_SELECTION_AI_FIRST_ZH_TW.md` |
| `analysis_pipeline`／AI 呼叫、快取、修復、分析流程 | `inktime/app/services/analysis.py`、`inktime/app/domain/analysis/plan.py` | `docs/reference/TOKEN_COST_GUIDE_ZH_TW.md` |
| `providers_usage`／Provider、模型、重試、帳務 | `inktime/app/providers/router.py`、該 Provider 檔案、`inktime/app/services/usage_tracking.py`、`budgets.py`（同目錄） | `docs/providers/OPENROUTER_ZH_TW.md`、`docs/guides/ACTIVITY_AI_TRACE_ZH_TW.md` |
| `openai_batch`／OpenAI Batch 狀態、取消、恢復 | `inktime/app/services/batch_analysis.py`、`inktime/app/providers/openai_batch.py` | `docs/OPENAI_BATCH_ANALYSIS_ZH_TW.md` |
| `jobs_workers`／Job、Worker、排程沒有動 | `inktime/app/workers/runner.py`、`scheduler.py`（同目錄）、`inktime/app/services/jobs.py` | `docs/guides/ACTIVITY_AI_TRACE_ZH_TW.md` |
| `selection_release`／候選資格、歷史選片、自動發布 | `inktime/app/repositories/render_candidates.py`、`inktime/app/services/display_prepare.py` | `docs/analysis/PHOTO_SELECTION_AI_FIRST_ZH_TW.md` |
| `rendering_release`／版型、色彩、預覽、發布回滾 | `inktime/app/services/rendering.py`、`release_coordinator.py`（同目錄）、`inktime/app/domain/rendering/` | `docs/architecture/ARCHITECTURE_ZH_TW.md` 對應渲染區段 |
| `web_settings`／Web、設定、權限、HTTP | 對應 `inktime/app/api/` route → `services/`；畫面讀 `inktime/app/web/templates/` 對應模板 | `docs/guides/ADMIN_GUIDE_ZH_TW.md` 對應功能；設定預設查 `inktime/app/repositories/settings.py` |
| `database_retention`／DB、Migration、資料保留 | `inktime/app/db/migrations.py`、對應 `inktime/app/repositories/` | `docs/operations/MIGRATION_GUIDE_ZH_TW.md`；已發布 migration 不可改寫 |
| `devices_pairing_queue`／配對、Queue、ACK、裝置 Release | `inktime/app/api/devices.py`、`device_pairing.py`（同目錄）、`inktime/app/services/device_queue_manifests.py` | `docs/devices/ESP32_AUTOMATIC_PAIRING_ZH_TW.md` |
| `photopainter_hardware`／ESP32、PhotoPainter、電源、Flash | `esp32/ink-display-7C-photo/ink-display-7C-photo.ino`、`photopainter_support.cpp`（同目錄） | **先完整讀 AGENTS.md 指定的兩份硬體文件**，再讀相關韌體；硬體安全閱讀不縮減 |
| `operations_ci`／NAS、Docker、備份、CI 失敗 | 對應 Compose／`scripts/`；`.github/workflows/ci.yml` 與失敗 job 實際 log | `docs/CI_POLICY.md`、`docs/operations/NAS_TAG_DEPLOYMENT_ZH_TW.md` |
| `documentation_baseline`／文件版本過期 | 下方權威來源 → `docs/reference/CURRENT_STATE_ZH_TW.md` 對應區段 | 同步中英文 README、`docs/README.md` 與 HTML 手冊相關段落；歷史報告保留日期 |

## 容易誤讀的契約與權威來源

- 版本與預設值集中在 [現行基線](reference/CURRENT_STATE_ZH_TW.md)。本頁不複製全部版本數字，避免再產生過期副本；實際部署另查診斷 Git revision。
- Migration 以 `inktime/app/db/migrations.py` 的 `MIGRATIONS` 清單為準；runner 出現 `migration.version == 58` 不代表最高只有 58。
- 排名以 `scoring.py` 的 `DEFAULT_RANKING_WEIGHTS` 與 `plan.py` 的 `ranking_weights` 為準；本機品質是資格門檻，不是 AI semantic ranking 的補位分數。
- 韌體版本查 `.ino` 的 `INKTIME_FIRMWARE_VERSION`；Config Store、Manifest、NAS contract 各自有版本，不可一起替換。
- `completed`、cache hit、發布成功、面板 ACK 是不同證據。程式與 CI 不能代表實板功耗或面板已驗收。

## Token 問題如何分流

**開發對話**：本頁限制不必要的檔案讀取與重複工具輸出。不要每次要求 agent「先讀完整專案」。此導航不保證特定節省比例，也不控制客戶端注入的歷史、工具定義或系統指令。

**InkTime 照片 API**：先查 AI Trace／usage 的 attempts、input/output/cached tokens、request type 與圖片大小，再定位 `analysis.py`。目前已有同計畫快取、一次圖片 Vision 與最多一次純文字修復；Provider 重試或新工作仍可能多次計費。不要只憑對話很長就修改照片分析 prompt、降低 Schema 或關閉必要驗證。詳見 [Token 與成本指南](reference/TOKEN_COST_GUIDE_ZH_TW.md)。

## 維護規則

新增／搬移模組時更新上表與 `AI_CONTEXT_INDEX.json`；版本或預設值變更時更新現行基線與對外入口。提交前執行 `python scripts/ci/validate_ai_navigation.py`，確認索引路徑、測試 glob、連結與大型檔案標記仍有效。只維護定位、契約與連結，不貼完整程式、巨大流程圖、歷史 log 或每次交付紀錄到 AGENTS.md／CLAUDE.md。
