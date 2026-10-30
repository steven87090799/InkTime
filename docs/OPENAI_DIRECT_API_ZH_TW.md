# OpenAI 官方 API：設定、費用與驗收

本功能在 InkTime 的 Python 後端新增 Responses Adapter，使用官方 `openai==3.41.0` SDK，直接呼叫 `https://api.openai.com/v1/responses`。即時／修復請求明確指定 Standard，避免繼承 Project 的 Fast 預設；Batch 不附加即時處理級別。OpenRouter 的 Chat Completions、模型 ID、免費路由、補齊專用 Provider 與既有分析資料繼續保留。ESP32 只接收後端產生的顯示內容，不接收 OpenAI Key。

## 真實資料流程與位置

照片讀取／掃描 → `domain/photos` 的 ThumbnailCache／PhotoPreprocessor → 既有 `services/analysis.py` 與凍結 Analysis Plan → Provider Adapter → 原有 v6 JSON 驗證、評分與 `_save_result` → SQLite／AI Cache／API Usage／AI Trace → 現有照片、工作與成本頁。

NAS Compose 的資料庫為 `/data/inktime.db`，宿主機對應 `${INKTIME_DATA_PATH}/inktime.db`；原始照片以唯讀方式掛載 `/photos`。實際宿主機位置取決於安裝設定。本次只修改獨立原始碼工作目錄，未部署或改寫 NAS 資料庫。新功能不需資料表 migration，也不更改評分規則；歷史 v4／v5／v6 結果仍由既有相容邏輯讀取。

## 變更檔案與責任

| 範圍 | 檔案與功能 |
| --- | --- |
| 新 Adapter／模型 | `inktime/app/providers/openai_responses.py`：SDK、Responses 請求／回應、模型權限查詢；`openai_models.py`：已核對能力與價格 |
| 共用 Provider | `providers/config.py`：獨立選項與官方端點限制；`providers/openai_compatible.py`：共用傳輸 hook、Token 計價、費用錯誤與日誌遮蔽；`services/providers.py`：Adapter 選擇、帳戶身分及路由隔離 |
| 分析／恢復 | `services/analysis.py`：凍結獨立模型／推理設定、確定拒絕後安全恢復；`workers/job_worker.py`：額度暫停；`domain/jobs/failure_policy.py`：不重試額度錯誤 |
| Batch／持久化 | `services/batch_analysis.py`：Responses endpoint、結果匯入、同意與分片暫停；`repositories/analysis_batches.py`：分片計數／費用分攤及待恢復標記；`repositories/billable_operations.py`：確定拒絕記錄；`repositories/providers.py`：新設定預設、金鑰分離與價格 |
| 成本／介面 | 新增 `services/openai_costs.py`；修改 `api/settings.py`、`api/batches.py`、`web/error_messages.py` 與 `providers.html`／`costs.html`／`analysis_batches.html` |
| 測試／依賴 | 新增 `tests/unit/test_openai_responses.py`、`tests/integration/test_openai_direct.py`；更新兩個舊 Mock 的官方 URL、CI 測試歸屬與兩架構 runtime locks，加入官方 SDK |
| 文件 | 本指南與 `docs/OPENAI_BATCH_ANALYSIS_ZH_TW.md` |

## 新增 OpenAI Provider

1. 在 OpenAI API Platform 選擇**已連結 Business 額度的 Organization**，於該組織的 Project 建立 API Key。Key 必須有 Responses、Models 存取權；使用 Batch 時也需 Files／Batches 權限。
2. InkTime「模型與 API」→「新增 Provider」，類型選 OpenAI，Base URL 固定為 `https://api.openai.com/v1`。API 模式選 Responses，模型預設 `gpt-6-luna`，推理強度預設 `none`。可選 `low` 等該模型支援的選項；輸出上限沿用既有設定，低上限搭配推理可能截斷，截斷結果不會自動重新送圖。
3. 輸入獨立的 OpenAI Key。Organization ID／Project ID 可留空使用 Key 本身的權限；明確填入時會送 `OpenAI-Organization`／`OpenAI-Project`，並納入 Batch 帳戶身分驗證。不要輸入 ChatGPT Cookie、登入 Token 或 OpenRouter Key。
4. 儲存後點「查驗已儲存設定的帳戶模型」。這只呼叫 `/models`，不傳圖片、不呼叫付費推論；清單與已核對官方能力的模型交集才標記可用。清單出現不等於完整 Vision 契約已驗收，可自行選 Level 2／3 合成圖片測試。這兩種測試有明確費用確認、有限輸出及呼叫次數。
5. 建議先維持並行 2、20 RPM，再依帳戶實際限制調整。可設定 TPM、逾時與冷卻；不會自動提高帳戶限額。

模型能力與價格依 2026-10-30 官方文件核對，包含 `gpt-6-luna`、`gpt-6.1-terra`、`gpt-6.1-sol`、`gpt-6-astra`。後兩者不支援 `none`；介面與 Adapter 會檢查參數。未知模型／快照需先核對能力並更新 catalog，不會猜測支援的參數。Key 未提供前，不能驗證你帳戶的模型權限。

新建 OpenAI Provider 預設 Responses；既有 OpenAI 設定若未指定 `options.api_mode`，繼續使用 Chat Completions，避免改變未完成工作的 API 契約。需要轉換既有設定時，建議另建 Provider；更改原 Provider 的模型、API 模式、組織或專案會觸發原有凍結設定保護，既有工作可暫停，但資料與進度不會刪除。

Key 以既有 SecretStore 的 Fernet 加密儲存，兩種服務依 `provider.<id>.api_key` 完全分開。設定頁只回傳遮罩。不要將 Key 放入 Git、前端、Compose 範例、韌體或一般日誌。解密主密鑰仍需依現有備份流程保存；本功能不搬移／重設主密鑰。

## 切換 OpenRouter／OpenAI

保留兩筆獨立 Provider，分別設定模型、金鑰與參數。將目標 Provider 啟用並設為較小優先值；也可停用不需要的 Provider。設定只供新工作使用，已有工作保留原凍結計畫。

OpenAI 是主要 Provider 時，Router 僅使用這個連線；冷卻、額度或限額問題不會讓下一張照片改用其他可能付費的 Provider。OpenRouter 仍保有原有路由／fallback 行為。OpenRouter 的免費補齊功能不會因新增 OpenAI 而改為付費分析。

## Business 額度與官方支出控制

免費 API 額度只適用於已連結的 API Organization，帳務由 OpenAI 扣抵。應用程式不處理 ChatGPT 登入，也不自行扣抵贈送額度。官方說明：[API credits for ChatGPT subscribers](https://developers.openai.com/api/docs/guides/chatgpt-api-credits)。

InkTime 沒有以一般 Project Key 查詢這項福利剩餘額度的正式介面，因此顯示「需至 OpenAI Billing 查詢」，餘額欄位為 null。金額是模型推論估算或供應商 usage 回報，**不代表使用者實際付款**。免費額度用完但另有付費餘額時，OpenAI 可能繼續使用付費餘額，InkTime 無法只花贈送額度。

官方 [Spend limits](https://developers.openai.com/api/docs/guides/spend-limits) 設定方式：

- Organization：API Platform → Organization limits → Spend → Edit spend limit → Monthly spend limit → 開啟 **Enforce a hard limit** → Save。
- Project：API Platform → 選擇 Project → Limits → Spend → Edit spend limit → 設定金額並開啟 **Enforce a hard limit**。
- Spend alerts 只通知，沒有停止效果。Hard limit 會回傳 429／`organization_spend_limit_exceeded` 或 `project_spend_limit_exceeded`；官方執行有傳播延遲，仍可能略超過門檻。

InkTime 另外沿用每日／每月警戒與停止值、每張上限、Job／Batch 預算，以及執行中請求成本預留。設定在「設定 → 預算」，Batch 提交表單也可填本批次預算。預留加上新工作超過上限時停止新增工作，保留原進度；它是輔助保護，無法保證所有執行中／未知結果／外部呼叫都不超支。

`credit_balance_exhausted`、`insufficient_quota`、Organization／Project 支出上限與組織用量限制歸為 `API_CREDIT_EXHAUSTED`，暫停並保留照片排隊，不自動轉用其他服務。Rate Limit、驗證失敗、模型不存在、暫時 5xx 各自保留原有錯誤分類。確定的 OpenAI 4xx 拒絕不被標成未知付費操作；修正後可恢復。逾時／送出後斷線／5xx 結果未知則不自動重送，必須先對帳。

## Token 與費用

逐張 usage 沿用 `api_usage`，記錄模型、Input／Output／Cached／Reasoning／Cache Write Token、延遲、估算費用、費用來源與錯誤；每次請求可從照片工作與 AI Trace 檢查。Reasoning 已包含於 Output，不重複加價。無可信 usage 或價格時保留 unknown，不能當作零。

新的 Responses Provider 讀取 2026-10-30 官方價格 catalog 作為預設，管理員的模型價格優先。`gpt-6-luna` 短上下文 Standard 每百萬 Token：Input US$0.10、Cached Input US$0.01、Output US$0.50、預設 30m Cache Write US$0.125；Batch 使用 0.5 倍。Cache Write 取代該部分一般輸入費用，不重複計價。此 Adapter 不設定 12h cache 或區域處理。自訂價格缺少 Cache Write 價格時，含寫入 Token 的結果會保留 unknown。超過 272K 單次輸入的 catalog 情境套用官方長上下文倍率。價格可能變動，提交前請核對 [API pricing](https://developers.openai.com/api/docs/pricing)。

成本頁新增最近最多 500 張保留樣本的 Token 情境，列出 1 萬／10 萬張 Standard 與 Batch 費用；樣本包含文字修復與已量測的重試費用。優先用相同 OpenAI 模型資料，若只有其他模型則明確標成跨模型情境，圖片 Token 與輸出長度需少量實測校正。含未知 Token 操作的照片不作完整樣本；沒有完整樣本就不顯示假設金額。`GET /api/v1/costs/openai-projection?remaining=<張數>` 可依樣本估算剩餘張數；Batch 詳情原有總成本、平均單張與缺件費用繼續保留。

本次可讀的本機資料庫沒有已回報 Token 的紀錄；既有 NAS 匯出摘要也沒有完整 Input／Output Token，因此無法聲稱以下是目前 NAS 實測成本。依專案既有即時估算基準 **8,000 Input＋512 Output／張**，不計快取、重試、額度扣抵及其他服務：

| 張數 | Standard 情境 | Batch 情境 |
| --- | ---: | ---: |
| 10,000 | US$10.56 | US$5.28 |
| 100,000 | US$105.60 | US$52.80 |

公式：`張數 × (Input × 0.10 + Output × 0.50) / 1,000,000`。這個 8,000 輸入是假設而非實測圖片 Token；實際成本頁會在使用者自行完成少量測試後使用保存的量測值。

## Batch 操作與隱私

「模型與 API」勾選 OpenAI Provider 支援 Batch，再到「Batch 照片分析」估算、設定預算並明確勾選 Files／Batch 上傳與暫存同意。新 Responses 提交在服務端也檢查同意，未同意時不建立工作、不讀取候選照片縮圖、不上傳。重試失敗項目沿用原本已保存的同意；切換到新的資料傳送模式時仍需取得同意。

每張縮圖是一行獨立 `/v1/responses`，共用相同 prompt、JSON Schema、影像前處理與資料庫寫入。每張穩定對應本機 Photo ID、內容 SHA 與匿名 custom_id，結果不依回傳順序。每片預設最多 500 張／150 MiB，逐張讀圖，沿用速率、預算、持久化 reservation 與未知提交保護。舊 Chat Completions Batch 的 endpoint 仍由既有紀錄保存，恢復時驗證該批次原 endpoint。

另修正既有多分片計數錯誤：父批次在第一片就提前縮小總數，後續拆第三片可能扣成負值。現在拆出子片時才扣除父片照片數，費用也依相同比例分攤。額度拒絕會暫停父工作，尚未上傳分片保留本機並持久標記；Scheduler 不會上傳，管理員恢復後才續跑。已提交分片仍可回收結果，不因暫停而丟棄。

原有建立、輪詢、下載、部分成功、取消、失敗限定重試、重啟恢復、重複匯入保護與遠端檔案清理沿用 [Batch 操作指南](OPENAI_BATCH_ANALYSIS_ZH_TW.md)。取消不能撤銷已執行費用，也不等於遠端檔案已即時刪除。

`store:false` 用於每個 Responses 請求，但 **不等於零資料保留**：Files／Batch 有獨立應用狀態與保留規則，預設濫用監測亦可能保留內容。匯入後會清理遠端 input／output／error 檔案；失敗時持久保留 cleanup_pending，之後重試清理。政策見 [Data controls](https://developers.openai.com/api/docs/guides/your-data) 及 [Batch API](https://developers.openai.com/api/docs/guides/batch)。

## 驗證範圍

新增離線測試覆蓋官方 SDK 實際序列化、單圖片／Schema／store、模型能力、獨立 Key 與標頭、帳戶模型查詢、原資料表寫入、Token 計價、驗證／模型／429／額度／5xx／逾時／異常 JSON／截斷／拒答、未知請求不重送、額度暫停後恢復、Batch 同意、亂序、部分成功、重建服務後匯入、重複匯入、只重試失敗項目，以及 OpenRouter 舊請求契約。

2026-10-30 本機離線驗證紀錄：

- Python 全量單元／整合／安全測試執行 3,664 項；初輪 31 項失敗集中於舊 Mock 使用非官方 OpenAI 網址、新錯誤碼說明與新測試的 CI 歸屬。已修正，並確認這 31 項全部包含於成功的後續重測。
- 最終相關回歸 932 項全部通過，包含既有 Batch、OpenRouter、預算、Trace 與金鑰保護。
- 最新 OpenAI 專用單元／整合測試 47 項全部通過，包含多分片費用分攤、額度暫停／恢復與舊 OpenRouter 類型正規化保留金鑰。
- Ruff、195 個來源檔的 mypy、文件導覽契約、依賴政策及兩個修改頁面的 JavaScript 語法檢查通過。
- 鎖定的 36 個 runtime 依賴經 pip-audit 檢查，未發現已知漏洞。

完整結果以 PR 的測試紀錄／CI 為準。本次未使用真實 Key、未向 NAS 私人照片執行付費測試、未建立真實遠端 Batch。尚待使用者自選非敏感樣本驗證帳戶權限、實際 Vision 品質、帳單／贈送額度扣抵、真實 Batch 取消與清理。不得把 Mock、離線測試或 CI 當成外部帳務與真機驗收。
