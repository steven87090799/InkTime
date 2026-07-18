# InkTime 專案架構與照片評分流程

這份文件是閱讀程式碼的入口。先看「執行架構」，再依要修改的功能查「模組地圖」；照片評分、模型與門檻集中在後半段。

## 執行架構

```mermaid
flowchart TB
    subgraph clients["使用端"]
        BROWSER["瀏覽器管理平台"]
        DEVICE["ESP32 電子紙"]
    end

    subgraph containers["Docker Compose"]
        WEB["inktime-web<br/>Gunicorn + Flask"]
        WORKER["inktime-worker<br/>有界背景工作"]
        SCHEDULER["inktime-scheduler<br/>排程與租約回收"]
    end

    subgraph layers["應用程式分層"]
        ROUTE["api／web<br/>驗證 HTTP、角色與 CSRF"]
        SERVICE["services<br/>流程與商業規則"]
        DOMAIN["domain<br/>純圖片／Schema／渲染邏輯"]
        REPOSITORY["repositories<br/>集中 SQL"]
        PROVIDER["providers<br/>視覺模型與用量"]
    end

    subgraph persistence["持久化與外部資源"]
        SQLITE[("SQLite WAL")]
        PHOTO["/photos 唯讀照片庫"]
        DATA["/data<br/>縮圖、字型、備份、Releases"]
        MODEL["OpenAI／相容／本地模型端點"]
    end

    BROWSER --> WEB
    DEVICE -->|"Bearer Token"| WEB
    WEB --> ROUTE --> SERVICE
    WORKER --> SERVICE
    SCHEDULER --> SERVICE
    SERVICE --> DOMAIN
    SERVICE --> REPOSITORY --> SQLITE
    SERVICE --> PROVIDER --> MODEL
    DOMAIN --> PHOTO
    DOMAIN --> DATA
```

三個容器共用同一個映像與 `/data`，但責任不同：Web 不執行長時間圖片工作；Worker 從 SQLite 領取有租約的工作；Scheduler 處理排程、恢復與備份。

## 模組地圖

| 想修改的功能 | 先看哪裡 | 下一層 |
|---|---|---|
| 登入、權限、CSRF | `inktime/app/api/auth.py`、`web/access.py` | `repositories/auth.py`、`core/security.py` |
| 照片掃描與本地特徵 | `workers/scanner.py` | `domain/photos/preprocessing.py`、`repositories/photos.py` |
| 模型分析與兩階段判斷 | `services/analysis.py` | `providers/openai_compatible.py`、`domain/analysis/schema.py` |
| 評分規則、權重、測試與還原 | `api/scoring.py`、`services/scoring_lab.py` | `repositories/scoring.py`、`domain/analysis/scoring.py` |
| 背景工作、暫停與恢復 | `workers/runner.py`、`workers/job_worker.py` | `repositories/jobs.py` |
| 模型路由、限流與熔斷 | `providers/router.py` | `services/providers.py`、`repositories/providers.py` |
| Token、成本與停止線 | `services/budgets.py` | `repositories/usage.py`、`repositories/settings.py` |
| 電子紙渲染與發布 | `services/rendering.py` | `domain/rendering/`、`api/rendering.py` |
| 裝置 Token 與下載 | `api/devices.py` | `repositories/devices.py`、`esp32/` |
| 管理介面 | `web/templates/` | `web/static/`、對應的 `api/*.py` |
| Docker 與啟動 | `docker-compose.yml`、`Dockerfile` | `server.py`、`platform.py` |

## 照片從掃描到發布

```mermaid
flowchart LR
    SCAN["掃描照片"] --> LOCAL["本地預處理<br/>EXIF、SHA-256、pHash、清晰度"]
    LOCAL --> DUP{"相同 SHA-256<br/>已有分析？"}
    DUP -->|"是"| INHERIT["繼承結果<br/>不呼叫模型"]
    DUP -->|"否"| STRATEGY{"分析策略"}
    STRATEGY -->|"local"| LOCAL_SCORE["本地固定公式"]
    STRATEGY -->|"low_cost／smart"| STAGE1["512px 第一階段模型"]
    STRATEGY -->|"high_quality"| STAGE2["1600px 高品質模型"]
    STAGE1 --> GATE{"回憶分達門檻<br/>或人物／最愛？"}
    GATE -->|"是"| STAGE2
    GATE -->|"否"| SAVE["保存四項原始分數、綜合分與規則版本"]
    STAGE2 --> SAVE
    LOCAL_SCORE --> SAVE
    INHERIT --> SAVE
    SAVE --> PICK["回憶分通過門檻<br/>依綜合分排序"] --> RELEASE["480×800 四色 2bpp Release"]
    RELEASE --> DEVICE["ESP32 驗證 SHA-256 後顯示"]
```

## 評分與「權重」的實際狀態

模型一次回傳四個獨立分數：

| 分數 | 意義 | 現在由誰決定 |
|---|---|---|
| `memory_score` | 值得回憶程度 | 視覺模型依固定 Prompt 判斷 |
| `beauty_score` | 美觀程度 | 視覺模型依固定 Prompt 判斷 |
| `technical_quality_score` | 清晰、曝光、構圖等技術品質 | 視覺模型依固定 Prompt 判斷 |
| `emotion_score` | 情緒與故事性 | 視覺模型依固定 Prompt 判斷 |

`memory_score` 不是加權總分；它是模型直接輸出的回憶分。系統另外保存 `ranking_score`：預設以回憶 50%、美觀 20%、技術品質 10%、情緒 20% 計算，最愛照片再加 5 分並限制在 0–100。管理員可在「評分」頁調整權重；四項必須合計 100%。`analysis.stage_two_threshold` 與 `render.memory_threshold` 仍是門檻，不是權重。

### 不改程式碼可以調整的項目

登入管理平台後，「設定」與「評分」各自負責：

| 設定鍵 | 用途 | 預設值 |
|---|---|---:|
| `model.low_model` | 第一階段低成本模型 | `gpt-4o-mini` |
| `model.high_model` | 第二階段高品質模型 | `gpt-4o` |
| 「評分」控制中心 | 規則、權重、最愛加分、版本歷史與單張測試 | 內建完整舊版規則 |
| `analysis.stage_two_threshold` | 第一階段回憶分達此值才升級；人物／最愛例外 | 65 |
| `render.memory_threshold` | 電子紙候選照片最低回憶分 | 70 |

建立工作時可在「工作」頁選擇 `local`、`low_cost`、`high_quality` 或 `smart_two_stage`。Provider、Base URL、API Key、價格與優先順序在「模型」頁管理。

### 要改評分規則時看哪裡

- 可編輯評分規則與綜合權重：管理介面「評分」頁；儲存時建立不可覆寫的歷史版本。
- 單張測試：`api/scoring.py` 暫存與刪除上傳檔，`services/scoring_lab.py` 呼叫目前高品質模型並記錄用量。
- 版本保存與還原：`repositories/scoring.py` 與 SQLite `scoring_rule_versions`。
- 評分規則版本化預設：`inktime/app/domain/analysis/scoring.py` 的 `DEFAULT_SCORING_RULES`。
- 不可由網頁覆寫的 JSON／語言／防虛構指令：`inktime/app/providers/openai_compatible.py` 的 `SYSTEM_PROMPT`。
- 分數欄位、型別與 0–100 範圍：`inktime/app/domain/analysis/schema.py`。
- 兩階段門檻判斷：`inktime/app/services/analysis.py` 的 `requires_second`。
- 僅本地策略的固定分數公式：同檔案的 `_local_result()`。
- Worker 如何讀取設定：`inktime/app/workers/runner.py`。
- 電子紙自動選片排序：`inktime/app/services/rendering.py`。
- 舊版原始回憶分／美觀分細則仍保留在 `legacy_analyze_photos.py`；其有效規則已整理為新版預設。

新規則只套用到之後的分析；既有照片保留當時的 `ranking_score` 與 `scoring_version_id`，不會在滑桿變動時悄悄改分。若要回溯比較，應另開重新分析工作。

## 設定與資料流

一般設定保存在 SQLite 的 `settings`／`setting_history`，定義與驗證位於 `repositories/settings.py`；API Key 等敏感值保存在加密的 `secrets`。`.env` 只放部署層路徑、Cookie 與 Log 等啟動參數，不是日常模型評分設定。

```mermaid
flowchart LR
    SETTINGS_UI["評分控制中心<br/>規則、權重、版本"] --> SETTINGS_API["POST /api/v1/scoring/profiles"]
    SETTINGS_API --> VALIDATE["ScoringProfileRepository 驗證與交易"]
    VALIDATE --> SETTINGS_DB[("settings")]
    VALIDATE --> VERSIONS[("scoring_rule_versions")]
    VALIDATE --> HISTORY[("setting_history")]
    WORKER["Worker 啟動工作"] --> SETTINGS_DB
    WORKER --> ANALYSIS["PhotoAnalysisService"]
```

## 建議閱讀順序

1. `README.md`：功能、部署與主要入口。
2. 本文件：執行架構、模組地圖與評分流程。
3. `inktime/app/platform.py`：應用程式如何組裝 Repository、Service 與 Blueprint。
4. 依上方模組地圖進入目標功能。
5. `docs/FINAL_IMPLEMENTATION_REPORT_ZH_TW.md`：完成證據與已知限制。
