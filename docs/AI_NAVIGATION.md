# AI 導航：僅 DISCOVERY / FULL_AUDIT 使用

若使用者已提供 file、symbol、function、line、error、failing test、PR、commit
或已知 subsystem：**不要讀本文件，不要跑 ai_context.py**，直接使用 TARGETED。
遵守 [AGENTS.md](../AGENTS.md)；[索引](AI_CONTEXT_INDEX.json) 是機器路由資料，無須全文讀取。

```text
TARGETED → exact rg → bounded read → patch → residual rg → lightweight validation
DISCOVERY → AI_NAVIGATION → one ai_context route → exact rg → bounded read → patch
FULL_AUDIT → subsystem-by-subsystem → compact conclusion per subsystem
           → never load whole repository at once
```

未知位置時只選一條 route：`python3 scripts/ci/ai_context.py <task-id> --max-items 6`。
預設只輸出候選路徑與下一個 rg 指令，不開啟候選原始碼。需要符號提示才加
`--symbols --max-items 6`；contracts/tests 都是按需線索，非必讀清單。
首輪最多 3 檔／300 行，單次輸出通常不超過 120 行；不足再以具體依據擴大。
每次擴大閱讀都要有依賴、錯誤或契約證據。已知畫面或 repository 不必經範例路由繞路。

| Route | 用途 |
|---|---|
| `startup` | inktime/app/factory.py |
| `scanner_photos` | inktime/app/workers/scanner.py |
| `openai_batch` | inktime/app/services/batch_analysis.py |
| `jobs_workers` | inktime/app/workers/runner.py |
| `analysis_prompt_schema` | Prompt assembly and current output schema |
| `analysis_ranking` | 67/33 weights, special bonus and favorite |
| `analysis_runtime` | Vision lifecycle, cache, retry and persistence |
| `providers_usage` | Locate active provider before reading usage/budget dependencies |
| `release_selection` | Candidate qualification and automatic release |
| `rendering` | Locate named renderer before domain helpers |
| `release_lifecycle` | Publication, rollback and release lifecycle |
| `web_ui` | Settings UI example; known screens should start directly at their own template |
| `settings_registry` | Search the exact setting key |
| `database_repository` | Photo repository example; use exact method in the affected repository |
| `database_migrations` | Locate exact migration version; preserve released migrations |
| `device_pairing` | Pairing endpoints and state |
| `device_offline_queue` | Offline manifests, queue and ACK |
| `hardware_safety` | Read safety contract before named firmware symbols |
| `ci_failure` | Start with exact failing job/error and testcase |
| `operations_runtime` | NAS runtime/update entry point; expand from actual error |
| `documentation_baseline` | docs/reference/CURRENT_STATE_ZH_TW.md |

## 使用邊界
- 所有 > 約 50 KB 文字檔與大型 tests 都先定位 symbol，再讀約 40–120 行。
- 不預載 tests、README、manual、archive、runtime、secrets 或 AI memory；需要才讀。
- PR diff-first，CI error → testcase → source；不重掃 main。
- PMIC/power/GPIO/boot/flash/storage bus/實板問題先讀
  [精簡硬體契約](devices/PHOTOPAINTER_SAFETY_CONTRACT.md)，歷史 handoff 僅在需 A/B 證據時開啟。
- 新功能／不同子系統用 AGENTS 的 compact handoff，建議新 chat；同任務重用證據。
- 此規則控制開發對話讀取，不能保證固定 Token 節省比例，也不修改照片 API 請求。

舊 route id 保留為索引中的 aliases，指向較窄入口；需要另一用途時選上表相應 route。
修改路由後執行 `python3 scripts/ci/validate_ai_navigation.py`。
