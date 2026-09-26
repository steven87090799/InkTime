# 開發與文件維護指南

本頁按需閱讀；AI 唯一規則入口是根目錄 [AGENTS.md](../../AGENTS.md)。已知檔案、符號、錯誤或 PR 時直接定位，不先讀完整 README、架構圖、文件地圖或測試套件。

## 用最少上下文完成修改

1. 在實際 Git 根目錄／隔離 worktree 確認狀態與 HEAD，保留其他人的修改；不要從包含多份 checkout 與 runtime data 的父目錄探索。
2. 用明確路徑加 `rg -n '符號或錯誤' path` 定位，再讀約 40–100 行；找檔名先用限定目錄的 `rg --files`。首輪最多 3 檔／300 行，不為填滿額度多讀。
3. 不知道位置才使用一條 [AI 修改導航](../AI_NAVIGATION.md) route。`python3 scripts/ci/ai_context.py <task-id>` 預設不讀候選原始碼；需要才加 `--symbols --max-items 6`。
4. 修改後檢查直接呼叫端、必要契約與相關測試。擴讀須有理由：輸入／輸出改變、失敗、共用狀態、相容性或安全問題；不要因省 Token 留下未追完的風險。
5. 工具輸出通常限制在 120 行。搜尋太廣先縮小路徑／符號，不重印遭截斷的整份結果。只重讀改過或新證據指向的段落。
6. 同一任務沿用已核對的事實。不同主題可用 `BASE_HEAD / FINAL_HEAD / PR / CHANGED_FILES / CONFIRMED_BEHAVIOR / UNRESOLVED / CI_STATUS` 簡短交接，再由使用者開新任務。

超過約 50 KB 的程式、tests、文件一律先定位符號／標題。測試依行為找案例與必要 fixtures；PR 從 diff 開始，CI 失敗從實際 job／error 開始。不預設使用子代理、抓整份 CI log 或重審所有 main。

## 預設不讀的資料

| 類別 | 常見路徑 | 何時擴讀 |
|---|---|---|
| Secrets | `.env*`、`session.key*` | 已明確定位的設定／恢復問題；優先讀 `.env.*.example`，不可回顯實際秘密 |
| Runtime | `data/`、`*.db`、`*.sqlite*`、`logs/`、`output/` | 程式、schema 與設定定義不足以解釋實際狀態；只查所需欄位／時間窗 |
| 歷史 | `docs/archive/`、日期命名修復報告、審查／效能報告 | 特定回歸、舊格式、歷史決策或量測證據 |
| 大型入口 | 中英文 README、`USER_MANUAL.html`、`docs/README.md` | 文件任務或指定操作段落；不是啟動必讀 |
| 媒體／產物／依賴 | 相簿、字型、圖片、ZIP、BIN、ELF、快取、`.venv`、`node_modules`、lockfile | 精確版本／資產／建置／硬體證據問題；先讀 metadata |
| 外部上下文 | 其他 worktree、對話、AI 記憶 | 目前任務確實需要的前次決策；不全庫載入 |

這些是代理閱讀規則，不是檔案權限或 sandbox；`.gitignore` 也不會禁止 AI 讀取。必要檔案可按上述條件讀取，硬體安全契約不可為省 Token 略過。

Codex 會從專案根目錄到工作目錄尋找 `AGENTS.md`，規則應保持精簡，詳細內容用按需連結。請在正確 repo／worktree 開啟新任務；尚未合併的規則不會自動套用到其他舊 checkout。載入機制見 [OpenAI 官方文件](https://learn.chatgpt.com/docs/agent-configuration/agents-md)。專案規則無法刪除客戶端注入的歷史、記憶、工具定義或系統指令，也無法承諾固定帳號額度節省比例。

## 架構與變更責任

Route 驗證 HTTP／角色／CSRF；Service 編排商業流程；Repository 管 SQL；Provider 管外部 API；Worker 管背景執行；domain 管純規則。禁止 Route 直接執行重型影像或模型呼叫。依涉及的層級選讀[架構文件](../architecture/ARCHITECTURE_ZH_TW.md)。

Released Migration 只能新增，不改寫已發布版本；最高版本以[現行基線](../reference/CURRENT_STATE_ZH_TW.md)及 `MIGRATIONS` 為準。維持 v4/v5 可讀、固定排名、正常照片單次 Vision 與未知付費請求不自動重送。測試 fixture 使用隔離資料與 Mock Provider，不依賴私人 NAS、真實 API 或家庭照片。

## 驗證與交付

```bash
git diff --check
# 只編譯本次改動的 Python 檔案，不 import 整個應用：
python3 -m py_compile scripts/ci/ai_context.py scripts/ci/validate_ai_navigation.py
# 文件／導航變更才執行此輕量檢查：
python3 scripts/ci/validate_ai_navigation.py
```

測試、建置、安全掃描、benchmark、瀏覽器與韌體編譯由 GitHub Actions 負責；不要把操作文件的部署命令當成例行開發驗證。工具未安裝不代表通過，不為文件修改安裝整套 runtime。

推送後查一次相應 Actions；queued／in progress 回報 `CI_PENDING`，不輪詢、重跑或手動 dispatch 製造通過。PR 維持 Draft；Ready、合併與 auto-merge 需明確授權。CI 的 merge-ref 與 source HEAD 分開記錄；詳見 [CI policy](../CI_POLICY.md)。

## 文件同步責任

| 變更內容 | 應同步 |
|---|---|
| 版本、預設、相容性 | 原始碼 → `CURRENT_STATE_ZH_TW.md` → 中英文 README 的摘要與相關指南 |
| 操作、頁面、部署或恢復步驟 | 相應指南；README／HTML 中受影響的操作範例 |
| 模組搬移／新增 | 直接入口、必要 architecture 說明與 `AI_CONTEXT_INDEX.json` route |
| 新增／搬移／刪除 Markdown | `docs/README.md` 與 `USER_MANUAL.html` 索引／相對連結 |
| 已完成的稽核／量測／交付 | 保留原日期、SHA、範圍與限制；標示歷史，不改成現況宣稱 |

`AGENTS.md` 保留唯一入口，不另建 `AGENT.md`／`CLAUDE.md`；版本表集中在基線，避免把每次驗證紀錄加到啟動規則。導航 validator 會檢查路由、閱讀預算、入口大小、基線版本、文件索引與現行 Markdown／HTML 本機連結。它不取代 runtime、Hosted CI 或硬體實測。
