# InkTime 模型 API 接入與控制台填寫指南

> 適用於目前 InkTime Web 管理介面，資料核對日期：2026-07-19。模型名稱、費率與 Rate Limit 會隨廠商調整；正式啟用前，請以各廠商控制台顯示的最新資料為準。

這份文件說明如何把雲端或本地視覺模型接入 InkTime，以及「模型」與「設定」頁的每個欄位應該怎麼填。InkTime 目前使用 **OpenAI Chat Completions 相容格式**傳送 JPEG 圖片，模型必須能同時完成：

- 接收 `messages[].content[]` 內的 Base64 `image_url`。
- 回傳 `choices[0].message.content`。
- 最好支援 `response_format.type=json_schema`；不支援時至少要穩定輸出 JSON。
- 最好回傳 `usage.prompt_tokens`／`usage.completion_tokens`，否則成本與 Token 統計會不完整。
- 提供 `GET /models`，因為 InkTime 的「測試」按鈕目前用這個端點檢查連線。

如果只想先測電子紙排版，不必先申請任何模型 API。可直接使用 InkTime「模擬器」或建立 `local` 策略工作；兩者都不會把照片傳給模型廠商。

## 先看：目前版本的五個重要限制

1. **Base URL 要填 API 根路徑**，例如 `https://api.openai.com/v1`。不要填完整的 `/chat/completions` URL，否則「測試」按鈕可能會錯誤地組出 `/chat/completions/models`。
2. 「類型」目前只是分類標籤。無論選 OpenAI、OpenAI 相容 API 或 Ollama，後端目前都使用同一個 OpenAI 相容轉接器；它不會把 Gemini、Claude、Bedrock 等原生格式自動轉換成 OpenAI 格式。
3. `model.low_model` 與 `model.high_model` 是**全系統共用**，不是每個 Provider 各自一組。若同時啟用 OpenAI 與 Gemini，InkTime 會把同一個模型 ID 傳給兩家；`gpt-4.1-mini` 在 Gemini 不存在，`gemini-...` 在 OpenAI 也不存在。因此目前建議一次只啟用一家直連廠商，或使用 OpenRouter 這類單一聚合端點。
4. 「測試」只代表 API Key、Base URL 與 `GET /models` 可連線，不代表圖片、JSON Schema 或模型名稱一定可用。儲存後仍要到「評分」頁用單張照片測試。
5. 資料庫已有模型價格欄位，但目前 Web 控制台尚未提供價格編輯表單。因此在控制台只能設定預算停止值，不能填每百萬 Token 單價；未另外寫入價格時，InkTime 的估計成本會是 `US$ 0`，**不代表廠商免費**。帳務仍以廠商控制台為準。

## 完整操作流程

### 第 1 步：先在模型廠商建立 API Key

1. 註冊廠商帳號並完成付款方式、儲值或免費額度設定。
2. 新增一把只給 InkTime 使用的 API Key，名稱可設為 `inktime`。
3. 若廠商允許，替 Key 設定每月金額上限、模型白名單與來源限制。
4. 複製 Key 後立即貼到 InkTime。多數廠商只會完整顯示一次。
5. 不要把 Key 寫入 `.env` 範例、Markdown、截圖、Git Commit 或聊天訊息。

照片會被傳到所選廠商進行分析。家庭照片若包含人物、住家、車牌、定位或兒童，請先閱讀廠商的資料保留、訓練使用、地區與刪除政策；若不能接受外傳，請選 Ollama／LM Studio 本地模型。

### 第 2 步：到 InkTime「模型」頁新增 Provider

登入管理員帳號，從上方導覽列進入「模型」，按「新增 Provider」。

| 控制台欄位 | 建議怎麼填 | 說明 |
|---|---|---|
| 名稱 | `OpenAI 主線`、`Gemini`、`本機 Ollama` | 只是 InkTime 顯示名稱，可自行命名。 |
| 類型 | 直連 OpenAI 選 `OpenAI`；其他雲端選 `OpenAI 相容 API`；Ollama／LM Studio 選 `本地 Ollama 相容端點` | 目前只影響標示，不改變 HTTP 格式。 |
| Base URL | 填到 `/v1` 或廠商指定的相容 API 根路徑 | 不要自行再加 `/chat/completions`。 |
| API Key | 貼上廠商提供的完整 Key | InkTime 加密儲存且頁面只顯示遮罩。編輯既有 Provider 時留空會保留舊 Key。 |
| 狀態 | 初次設定先選 `啟用` | 若同時設定不同廠商，先只啟用正在測試的一家。 |
| 優先順序 | 第一家 `100`、備援 `200`、第三家 `300` | 數字越小越先使用。只有各家接受相同模型 ID 時才適合直接備援。 |
| 最大並行數 | 先填 `1` | 通過實際測試後再提高到 `2`；Intel N100 與免費 API 層不建議一開始開高。 |
| 每分鐘 Request 上限 | 不確定就留空；知道方案限制才填 | 這是 InkTime 自己的保守限流，不會自動向廠商讀取。建議略低於廠商上限。 |
| 每分鐘 Token 上限 | 不確定就留空；知道方案限制才填 | 同上；填太低會讓工作等待，填太高仍可能收到廠商 429。 |
| HTTP 逾時秒數 | 雲端 `120`；本地小模型 `180`；本地大模型 `300–600` | 最大可填 600。圖片分析通常比純文字慢。 |
| 故障冷卻秒數 | `300` | 發生失敗或 429 後暫時避開此 Provider；若廠商有明確 `Retry-After`，InkTime 會尊重較長時間。 |
| 支援 Batch API | **先不要勾** | 目前背景工作的自動分批、結果匯入與斷點續跑尚未完成。即使廠商支援 Batch，也不代表大量工作會自動走 Batch。 |
| 支援嚴格 JSON Schema | 確定支援才勾；不確定先取消 | 勾選後 InkTime 會傳送 OpenAI 形式的 `response_format: json_schema`。不支援的端點通常回 400。取消後仍會在應用層驗證 JSON，失敗時最多做一次純文字修復。 |

儲存後按「測試」。看到「連線成功」後，還不能直接跑全相簿；請繼續完成模型名稱與單張照片測試。

### 第 3 步：到「設定」頁填模型名稱

進入「設定」→「模型設定」：

| 欄位 | 用途 | 建議 |
|---|---|---|
| `model.low_model` | 512px 第一階段、低成本初篩 | 填廠商模型清單中的**完整模型 ID**，不是網頁顯示名稱。 |
| `model.high_model` | 1600px 第二階段、高品質分析與評分測試台 | 可填較高品質模型；第一次接入可先與 low_model 填相同值，確認流程後再拆分。 |

模型 ID 必須原樣複製，包含大小寫、斜線、冒號與版本尾碼。例如 OpenRouter 常見 `廠商/模型`，Ollama 常見 `模型:尺寸`。不要把模型的中文名稱或產品頁標題填進去。

接著在「成本設定」先保守填入：

- 每日警告：`1`
- 每日停止：`2`
- 每月警告：`10`
- 每月停止：`20`
- 單一工作預算：`1`
- 單張照片上限：`0.10`
- 單次最大 Token：先保留 `8000`

這些數值只是首次測試範例，幣別為美元。因目前尚無 Web 價格輸入欄位，InkTime 可能無法正確換算金額；同時務必在廠商控制台設定真正的消費上限與用量通知。

### 第 4 步：做真正的單張驗收

1. 到「評分」頁上傳一張不敏感、內容清楚的測試照片。
2. 按「使用目前規則測試」。
3. 確認回傳繁體中文描述、`side_caption`、照片分類與四項 0–100 分數。
4. 到「成本」頁確認 Provider、模型名稱、Token 與請求紀錄有出現。
5. 再建立 5–10 張照片的小工作；確認沒有 400、401、404、429、Schema 錯誤或無限等待。
6. 小批次穩定後，才提高並行數或處理完整照片庫。

## 各廠商的控制台填寫方式

以下只列出可透過目前 OpenAI 相容轉接器接入、或使用者最常詢問的主要廠商。模型會下架或更名，因此表中的模型只作起始範例；最可靠方式永遠是從廠商的 Models 頁複製目前支援「圖片輸入」的模型 ID。

### 1. OpenAI：最直接、相容性最高

先到 [OpenAI API Keys](https://platform.openai.com/api-keys) 建立 Secret Key，並在 Billing／Limits 設定付款與用量上限。OpenAI 提供標準 Bearer 驗證與 `GET /v1/models`；可參考 [Models API](https://platform.openai.com/docs/api-reference/models/list)。

「模型」頁填法：

| 欄位 | 值 |
|---|---|
| 名稱 | `OpenAI` |
| 類型 | `OpenAI` |
| Base URL | `https://api.openai.com/v1` |
| API Key | `sk-...` 或控制台實際產生的 Key |
| 優先順序 | `100` |
| 最大並行 | `1` |
| HTTP 逾時 | `120` |
| 故障冷卻 | `300` |
| Batch | 不勾；目前 InkTime 大量工作仍走即時 API |
| 嚴格 JSON Schema | 勾選 |

「設定」頁可先填一組目前帳號確實有權限、支援圖片與 Chat Completions 的模型。若使用仍可用的 GPT-4.1 系列，可用 `gpt-4.1-mini` 作 low、`gpt-4.1` 作 high；若廠商已提供更新型號，請先確認它仍接受 InkTime 送出的 `temperature=0.1`、Chat Completions 圖片與 JSON Schema，再替換模型 ID。

### 2. Google Gemini API：使用 Google AI Studio Key

到 [Google AI Studio API Keys](https://aistudio.google.com/apikey) 建立 Key。不要填 Gemini 原生 `generateContent` URL；必須使用 Google 官方的 OpenAI 相容端點。官方範例與圖片格式見 [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)。

| 欄位 | 值 |
|---|---|
| 名稱 | `Google Gemini` |
| 類型 | `OpenAI 相容 API` |
| Base URL | `https://generativelanguage.googleapis.com/v1beta/openai` |
| API Key | AI Studio 產生的 Gemini API Key |
| 優先順序 | `100` |
| 最大並行 | 免費層先 `1` |
| RPM／TPM | 依 AI Studio 顯示的專案方案填；不確定留空 |
| HTTP 逾時 | `120` |
| Batch | 不勾 |
| 嚴格 JSON Schema | 可先勾；若實際照片請求回 400，再取消後測試 |

模型名稱從 Gemini 官方模型清單複製。官方相容文件目前以 `gemini-3.5-flash` 示範圖片輸入，可先把 low/high 都設成帳號中實際可用的同一個 Flash 視覺模型，再依需求把 high 改成品質較高的視覺模型。

### 3. OpenRouter：最適合一把 Key 使用多家模型

OpenRouter 把多家模型正規化成一個 OpenAI 相容端點，最能避開 InkTime「模型名稱全域共用」的限制。到 [OpenRouter Keys](https://openrouter.ai/settings/keys) 建立 Key，並設定 Credit Limit，再從 [Models](https://openrouter.ai/models) 篩選：

- Input modalities 包含 image。
- 支援 `structured_outputs`／`response_format`。
- Context 與輸出上限足以容納 InkTime Schema。
- 使用正式付費模型前先看資料處理與 Provider routing 政策。

官方驗證與 Base URL 見 [OpenRouter Authentication](https://openrouter.ai/docs/api/reference/authentication)，圖片與 Schema 格式見 [API Reference](https://openrouter.ai/docs/api/reference/overview)。

| 欄位 | 值 |
|---|---|
| 名稱 | `OpenRouter` |
| 類型 | `OpenAI 相容 API` |
| Base URL | `https://openrouter.ai/api/v1` |
| API Key | `sk-or-v1-...` |
| 優先順序 | `100` |
| 最大並行 | `1`，穩定後可試 `2` |
| HTTP 逾時 | `180` |
| 故障冷卻 | `300` |
| Batch | 不勾 |
| 嚴格 JSON Schema | 所選模型明確支援 structured outputs 才勾 |

模型 ID 要連同廠商前綴完整貼上，例如 `openai/...`、`google/...`、`anthropic/...`；不要只填尾端名稱。若 low/high 選不同廠商也沒問題，因為兩者仍由同一個 OpenRouter Base URL 處理。

### 4. Together AI：OpenAI 相容的多模型平台

在 Together 控制台建立 API Key，並從其 Model Catalog 選擇仍在服務中的 Vision 模型。官方相容矩陣確認支援 Chat Completions 圖片、Base64 data URI、Structured Outputs 與 `GET /models`，見 [Together OpenAI compatibility](https://docs.together.ai/docs/inference/openai-compatibility)。

| 欄位 | 值 |
|---|---|
| 名稱 | `Together AI` |
| 類型 | `OpenAI 相容 API` |
| Base URL | `https://api.together.ai/v1` |
| API Key | Together 控制台產生的 Key |
| 最大並行 | `1` |
| HTTP 逾時 | `180` |
| Batch | 不勾；Together 的 Batch 不是 InkTime 目前直接使用的 OpenAI Batch 流程 |
| 嚴格 JSON Schema | 所選 Vision 模型支援 Structured Outputs 時勾選 |

模型名稱通常是 `provider/model_name`。請從 Together 模型頁複製 exact ID；不要使用純文字模型，否則圖片會被拒絕或忽略。

### 5. Mistral AI：使用支援 Vision 的 Mistral 模型

到 [Mistral Console](https://console.mistral.ai/) 建立 API Key。Mistral 的 Chat Completions 與 OpenAI 結構相近，Base URL 可參考 [Mistral migration guide](https://docs.mistral.ai/resources/migration-guides)，目前支援視覺的模型與 Base64 圖片格式見 [Mistral Vision](https://docs.mistral.ai/studio-api/conversations/vision)。

| 欄位 | 值 |
|---|---|
| 名稱 | `Mistral AI` |
| 類型 | `OpenAI 相容 API` |
| Base URL | `https://api.mistral.ai/v1` |
| API Key | Mistral API Key |
| 最大並行 | `1` |
| HTTP 逾時 | `180` |
| Batch | 不勾 |
| 嚴格 JSON Schema | 先勾；若所選模型拒絕 InkTime Schema，再取消 |

官方目前列出的 Vision 型號包含 Mistral Small／Medium／Large 與 Ministral 的特定版本。可先把 low/high 都填成控制台仍可用的 `mistral-small-latest`；若 alias 行為或能力改變，改用 Models 頁所列的固定版本 ID。

### 6. Groq：必須先確認目前仍有可用 Vision 模型

到 [Groq API Keys](https://console.groq.com/keys) 建立 Key。Groq 的 Base URL 與 OpenAI 相容說明見 [Groq OpenAI Compatibility](https://console.groq.com/docs/openai)，模型 API 見 [Groq API Reference](https://console.groq.com/docs/api-reference)。

| 欄位 | 值 |
|---|---|
| 名稱 | `Groq` |
| 類型 | `OpenAI 相容 API` |
| Base URL | `https://api.groq.com/openai/v1` |
| API Key | `gsk_...` 或控制台實際 Key |
| 最大並行 | `1` |
| RPM／TPM | 依 Groq Limits 頁填，免費層不要猜測 |
| HTTP 逾時 | `120` |
| Batch | 不勾 |
| 嚴格 JSON Schema | 所選模型明確支援時才勾；否則先取消 |

Groq 模型上下架速度較快，而且曾下架多個 Llama Vision 型號。不要照抄舊文章的模型 ID；先在 Groq Models／Playground 確認該模型現在為 Active、接受圖片且能輸出 JSON。若當下沒有 Active Vision 模型，Groq 就不能直接用於 InkTime，即使「測試」按鈕能成功列出純文字模型。

### 7. Anthropic Claude：可試官方相容層，但不是首選正式路徑

到 [Claude Console](https://console.anthropic.com/) 建立 API Key。Anthropic 提供 `https://api.anthropic.com/v1/` 的 OpenAI SDK 相容層，見 [Claude OpenAI SDK compatibility](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)。但官方明確說明此相容層主要用於測試比較，不是大多數正式環境的長期方案，而且會忽略 `response_format`。

| 欄位 | 值 |
|---|---|
| 名稱 | `Anthropic Claude 相容層` |
| 類型 | `OpenAI 相容 API` |
| Base URL | `https://api.anthropic.com/v1` |
| API Key | Claude API Key |
| 最大並行 | `1` |
| HTTP 逾時 | `180` |
| Batch | 不勾 |
| 嚴格 JSON Schema | **取消勾選**；官方相容層會忽略 OpenAI `response_format` |

模型名稱請從 Claude Models 頁複製完整 ID，例如官方相容文件示範的 Sonnet 型號。先用單張照片確認相容層確實轉換圖片內容並穩定回傳 InkTime JSON；只要圖片被忽略、Schema 經常失敗或「測試」無法列模型，就先停用。若要正式、完整使用 Claude Native Structured Outputs，InkTime 需要另開發 Anthropic 原生 Provider，不能只靠控制台填值完成。

### 8. xAI Grok：可用 OpenAI 相容端點，須選支援圖片的型號

到 [xAI Console](https://console.x.ai/) 建立 API Key。xAI 使用 Bearer Key 與 `https://api.x.ai/v1`，其 Chat Completions 可處理圖片；參考 [xAI Chat API](https://docs.x.ai/developers/rest-api-reference/inference/chat)與 [Image Understanding](https://docs.x.ai/developers/model-capabilities/images/understanding)。

| 欄位 | 值 |
|---|---|
| 名稱 | `xAI Grok` |
| 類型 | `OpenAI 相容 API` |
| Base URL | `https://api.x.ai/v1` |
| API Key | xAI API Key |
| 最大並行 | `1` |
| HTTP 逾時 | `180`；推理型號需要時可提高 |
| Batch | 不勾 |
| 嚴格 JSON Schema | 所選 Chat 模型明確支援時勾；首次可先取消以降低相容風險 |

模型必須從 xAI Models 頁選支援 image input 與 Chat Completions 的型號。官方圖片文件目前示範 `grok-4.5`，但仍應以你的帳號權限與當期型號為準。

### 9. Alibaba Cloud Model Studio／Qwen：注意地區與 Workspace URL

在 Alibaba Cloud Model Studio 啟用服務並建立 API Key。官方相容文件見 [Call Qwen via OpenAI API](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope)。Qwen-VL 才是視覺模型；`qwen-plus` 等純文字模型不能替 InkTime 看照片。

| 欄位 | 值 |
|---|---|
| 名稱 | `Alibaba Qwen-VL` |
| 類型 | `OpenAI 相容 API` |
| Base URL | 依 Workspace 地區複製 `.../compatible-mode/v1` |
| API Key | 該地區／Workspace 的 Model Studio Key |
| 最大並行 | `1` |
| HTTP 逾時 | `180` |
| Batch | 不勾 |
| 嚴格 JSON Schema | 先取消；確認所選 Qwen-VL 型號支援相同 Schema 格式後再勾 |

國際版常見新式 Base URL 會包含 `{WorkspaceId}` 與地區，例如新加坡區域的 `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`。必須把 `{WorkspaceId}` 換成真實值，且 Key、模型可用區域與 URL 必須一致。模型名稱請從該區 Models 清單選 Qwen-VL 的完整 ID，例如帳號仍提供時可從 `qwen-vl-plus` 類型開始測試。

### 10. Ollama：照片不離開本機

先在執行 Ollama 的主機安裝並啟動服務，再拉取視覺模型。Ollama 官方相容文件目前以 `qwen3-vl:8b` 示範圖片輸入，並支援 `/v1/chat/completions`、`/v1/models`、Vision 與 `response_format`；見 [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)。

主機執行：

```bash
ollama pull qwen3-vl:8b
```

若 InkTime 直接在同一台主機、不是 Docker：

| 欄位 | 值 |
|---|---|
| 名稱 | `本機 Ollama` |
| 類型 | `本地 Ollama 相容端點` |
| Base URL | `http://127.0.0.1:11434/v1` |
| API Key | 可留空；未啟用驗證時 Ollama 會忽略 Key |
| 最大並行 | `1` |
| HTTP 逾時 | `300–600` |
| Batch | 不勾 |
| 嚴格 JSON Schema | 先勾；模型若無法遵守再取消 |

若 InkTime 在 Docker 而 Ollama 跑在宿主機，**不要填 `127.0.0.1`**，因為容器中的 localhost 是容器自己：

- macOS／Windows Docker Desktop：通常填 `http://host.docker.internal:11434/v1`。
- Ollama 與 InkTime 都在同一個 Compose network：填 Ollama service 名稱，例如 `http://ollama:11434/v1`。
- Linux：需先替容器設定可解析的 host gateway，或使用同一 Compose network；不要為了方便把 Ollama 無驗證地暴露到公網。

「設定」頁把 low/high 都填成 `ollama list` 顯示的完整 Vision 模型名稱，例如 `qwen3-vl:8b`。只有文字模型即使能通過 `/models` 測試，也無法完成 InkTime 照片分析。

### 11. LM Studio：本機 GUI 模型伺服器

在 LM Studio 下載一個支援 Vision 的模型，到 Developer 頁啟動 Local Server；需要時在 Server Settings 建立 API Token。LM Studio 提供 `/v1/chat/completions` 與 `/v1/models`，參考 [OpenAI-compatible Models API](https://lmstudio.ai/docs/developer/openai-compat/models)與 [Local Server](https://lmstudio.ai/docs/developer/core/server)。

| 欄位 | 值 |
|---|---|
| 名稱 | `本機 LM Studio` |
| 類型 | `本地 Ollama 相容端點` |
| Base URL | `http://127.0.0.1:1234/v1`；Docker 內改用 `http://host.docker.internal:1234/v1` |
| API Key | 未啟用驗證可留空；啟用後填 Developer 頁產生的 Token |
| 最大並行 | `1` |
| HTTP 逾時 | `300–600` |
| Batch | 不勾 |
| 嚴格 JSON Schema | 所載入模型與 LM Studio Structured Output 實測可用時勾選 |

模型名稱填 `GET /v1/models` 或 LM Studio Developer 頁顯示的 model ID。請確認模型類型是 VLM／支援 Vision，不是一般 LLM；同時確保 Context Length 足以容納圖片與 InkTime 的完整 JSON Schema。

## 目前不能直接靠控制台接入的廠商／介面

### DeepSeek 官方 API

DeepSeek 官方 API 雖然是 OpenAI 相容文字 Chat API，但目前官方快速入門只列文字模型與文字訊息，沒有可供 InkTime 使用的 image input 規格；見 [DeepSeek API Docs](https://api-docs.deepseek.com/)。因此 `https://api.deepseek.com` 即使連線成功，也不應直接用於照片分析。若要使用 DeepSeek 相關能力，必須等官方提供相容視覺模型，或改在 OpenRouter／Together／本地平台選一個真正支援圖片的 VLM。

### Azure OpenAI／Microsoft Foundry 傳統部署

傳統 Azure OpenAI 常需要 deployment-specific URL、`api-version` 查詢參數或 `api-key` Header；目前 InkTime 固定使用 `Authorization: Bearer`，也固定用 `GET /models` 測試，無法在控制台另外設定 Header 或 query string。因此不能保證只填 Base URL 與 Key 就能使用。新版 Foundry 若提供完整 OpenAI `/openai/v1` 相容端點、Bearer Key、`GET /models`、Chat Completions Vision 與 JSON Schema，才可依「其他相容端點」流程實測；否則需要開發 Azure 專用 Provider。

### AWS Bedrock

Bedrock 原生需要 AWS SigV4、Region、Access Key／Role 與不同的模型請求格式。InkTime 控制台目前只有單一 Bearer API Key 欄位，無法直接填入。可使用你自行部署、已完成 AWS 驗證與 OpenAI 格式轉換的安全 Gateway；不要把 AWS Secret Access Key 當成 API Key 貼入 InkTime。

### Google Vertex AI 原生端點

Vertex AI 常使用 OAuth／Service Account、Project、Location 與 publisher model 路徑，不等同 Google AI Studio 的 Gemini OpenAI 相容 URL。控制台目前不能上傳 Service Account JSON 或更新 OAuth Token，因此建議使用前述 Gemini Developer API 相容端點，或部署安全的 OpenAI 相容 Gateway。

### Cohere 與其他純文字／Embedding API

InkTime 需要 Vision，不是只要能聊天就能使用。任何只支援文字、Embedding、圖片生成而不支援圖片理解的模型，都不能作為 InkTime Provider。

## 其他 OpenAI 相容廠商的通用判斷與填法

Fireworks、NVIDIA NIM、自架 vLLM、LiteLLM Proxy 或其他平台若同時滿足以下條件，通常可以接入：

1. 驗證方式是 `Authorization: Bearer <API Key>`，或完全不需驗證。
2. 有 `GET {Base URL}/models`。
3. 有 `POST {Base URL}/chat/completions`。
4. 接受 `image_url.url = data:image/jpeg;base64,...`。
5. 回傳 OpenAI `choices[].message.content`。
6. 回傳 JSON 字串，最好支援 OpenAI 形式的 `response_format=json_schema`。
7. 所選模型確實支援 Vision，不只是平台本身宣稱 OpenAI-compatible。

控制台通用填法：類型選「OpenAI 相容 API」、Base URL 填到 `/v1` 根路徑、並行先 1、Batch 不勾、Schema 不確定就先取消。若廠商需要 `x-api-key`、自訂 Header、OAuth、AWS SigV4、mTLS、動態 Token 或非 OpenAI 圖片格式，目前就不能只靠控制台完成，必須新增專用 Provider 或在內網放一個受保護的轉接 Gateway。

## 多 Provider、優先順序與備援的正確用法

目前只有下列情況適合同時啟用多個 Provider：

- 多個 Provider 都接受完全相同的 low/high 模型 ID。
- 多個自架端點載入相同模型並使用相同別名。
- 主要與備援都由相同聚合平台／相容 Gateway 統一模型命名。

不建議直接同時啟用 OpenAI、Gemini、Claude 三家，因為模型 ID 不共通。若要切換廠商：

1. 暫停正在執行的模型工作。
2. 在「模型」頁停用舊 Provider、啟用新 Provider。
3. 到「設定」修改 low/high 模型 ID。
4. 執行新 Provider 的連線測試。
5. 到「評分」做單張實際測試。
6. 確認成功後再恢復或建立工作。

優先順序數字越小越先使用。發生 Provider 錯誤、429 或熔斷後才會嘗試下一個；它不會自動比較價格、畫質或隱私政策。

## 常見錯誤排查

| 現象 | 最可能原因 | 處理方式 |
|---|---|---|
| 測試顯示 HTTP 401／403 | Key 錯誤、沒有 Billing、Key 與地區不符、模型權限未開 | 重建專用 Key，檢查廠商 Billing、Project／Workspace、模型白名單。 |
| 測試顯示 HTTP 404 | Base URL 填到 `/chat/completions`、URL 少了 `/v1`、廠商沒有 `/models` | 改填官方 OpenAI-compatible 根路徑；若廠商確實沒有 `/models`，目前測試機制不相容。 |
| 測試成功，單張照片卻 400 | 模型不支援圖片、`detail`、`temperature=0.1` 或 JSON Schema | 先取消「嚴格 JSON Schema」再測；仍失敗就換真正的 Vision Chat Completions 模型。 |
| 回傳 VLM-003／VLM-004 | 模型輸出不是合法 JSON 或不符合 InkTime Schema | 使用支援 Structured Outputs 的模型；降低並行；不要選太小或不擅長指令遵循的本地模型。 |
| 回傳 429 | 廠商 Rate Limit／餘額限制 | 將最大並行降到 1，設定較低 RPM／TPM，提高冷卻秒數，或升級廠商方案。 |
| 本地端點無法連線 | Docker 容器內的 localhost 指錯位置，或本地服務只監聽 loopback | Docker 改用 service name／`host.docker.internal`，並檢查防火牆與服務監聽位址。 |
| 模型不存在 | 把顯示名稱當 ID、模型已下架、區域不提供、Provider 收到別家模型 ID | 從該 Provider 的 Models 頁重新複製 exact ID；一次只啟用一家直連廠商。 |
| 成本永遠是 US$ 0 | Web UI 目前無模型單價輸入欄位 | 以廠商帳務為準，先在廠商端設上限；不要把 InkTime 的 0 當免費。 |
| Key 編輯後仍顯示遮罩 | 正常安全行為 | 編輯時 Key 留空會保留舊 Key；只有要更換時才貼新 Key。 |

## 上線前檢查清單

- [ ] API Key 是 InkTime 專用，已設定廠商端金額限制與通知。
- [ ] 已確認照片資料保留、訓練使用、地區與隱私政策。
- [ ] Base URL 填根路徑，不含 `/chat/completions`。
- [ ] 目前只啟用模型 ID 相容的 Provider。
- [ ] low/high 都是完整、仍有效、支援圖片的模型 ID。
- [ ] 「測試」顯示連線成功。
- [ ] 「評分」單張照片能回繁體中文描述、分類、四項分數與短文案。
- [ ] 「成本」頁有 Token／Provider／模型紀錄，並已了解金額可能尚未正確換算。
- [ ] 已用 5–10 張照片的小工作驗證，而不是直接跑完整相簿。
- [ ] 免費層、本地模型或 N100 的最大並行先維持 1。
- [ ] Batch 保持關閉，直到背景自動分批與結果匯入完整實作。

完成以上檢查後，再依實際 429、延遲、記憶體與費用逐步提高並行或改用兩階段模型。任何 Provider 只通過 `/models` 測試，都不能算完成接入；**單張圖片 Schema 驗收與小批次工作成功才算真正可用**。
