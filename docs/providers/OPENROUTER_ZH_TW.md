# OpenRouter 正式 Provider 與隱私／路由設定

本文件對應目前程式的 `kind=openrouter` Provider。它不是另一套分析引擎，而是由 `OpenAICompatibleProvider` 以 OpenRouter 專用 request contract 傳送 Chat Completions。正式 API、模型 ID、資料處理政策與可用能力會變動；啟用前請以 [OpenRouter Models](https://openrouter.ai/models) 與 [官方 API 文件](https://openrouter.ai/docs/api/reference/overview) 為準。

## 控制台最小設定

在「模型與 API」頁新增 Provider：

| 欄位 | 建議值 | 注意 |
|---|---|---|
| 類型 | `OpenRouter（正式 API）` | 這個 kind 會啟用 routing、reasoning、usage cost 與 Batch hard guard。 |
| Base URL | `https://openrouter.ai/api/v1` | 必須是 HTTPS 根路徑；不要填 `/chat/completions`、query 或 fragment。 |
| 預設模型 ID | `openrouter/free` 或完整模型 ID | 留白沿用系統模型設定；填寫後該 Provider 的 Vision／text-only repair 都使用此模型。 |
| API Key | `sk-or-v1-...` | 只進入加密 Secret Store；不會放入分析計畫、Log 或診斷包。 |
| Batch | 不勾選 | OpenRouter 在 InkTime 內只走即時 Chat Completions，Batch API 會 server-side 拒絕。 |
| 嚴格 JSON Schema | 依所選模型能力 | 只有模型／路由明確支援 structured outputs 才勾選。 |
| 最大並行 | 先 `1` | 小批次驗收通過後再提高，並同步設定 RPM／TPM 保守上限。 |

模型 ID 必須保留完整 provider 前綴，例如 `openai/...`、`google/...`、`openrouter/free` 或 Models 頁列出的其他完整 ID。在 `/providers` 的 Provider 編輯視窗可直接輸入，或從建議值選擇「預設模型 ID」；該 Provider 的 Vision 與 text-only repair 都會使用它。留白會沿用系統 analysis model，但該值仍必須是完整 OpenRouter ID。InkTime 不會替短名稱自動補前綴；像 `gpt-4o` 這種短名稱會在產生工作、Provider 測試或外部請求前被拒絕，不會先送出可能付費的錯誤請求。

若 OpenRouter 回傳 HTTP 400，Provider Contract 測試會顯示經過遮蔽與長度限制的錯誤代碼、HTTP 狀態和 provider message，方便判斷模型或請求格式問題；API Key、圖片資料與完整原始回應不會出現在畫面上。

## `options` JSON

Provider 進階選項存放在 `providers.options_json`，API 與子程序共用同一個 allowlist。可以先從最小隱私設定開始：

```json
{
  "data_collection": "deny",
  "zdr": true,
  "allow_fallbacks": false,
  "require_parameters": true,
  "session_sticky": true,
  "http_referer": "https://photos.example.invalid",
  "app_title": "InkTime"
}
```

目前可用欄位：

- `order`、`only`、`ignore`：Provider routing 限制；`order` 不可與 `only`／`ignore` 同時存在，`only` 與 `ignore` 也不可重疊。
- `allow_fallbacks`、`require_parameters`、`zdr`、`session_sticky`：Boolean。若未明確提供 `require_parameters`，InkTime 會保存並送出 `true`；只有管理員明確設為 `false` 才會關閉這項路由限制。
- `data_collection`：`allow` 或 `deny`。若家庭照片不應進入可選資料收集路徑，使用 `deny`，並仍核對模型／路由實際政策。
- `quantizations`：量化名稱陣列。
- `sort`：`price`、`throughput`、`latency`，或含 `by`／`partition` 的合法 object。
- `preferred_min_throughput`、`preferred_max_latency`：可填有限非負數，或 `p50`／`p75`／`p90`／`p99` percentile object。
- `max_price`：可填 `prompt`、`completion`、`request`、`image` 的有限非負數 object；這是 current nested shape，不使用舊的 `max_price_input`／`max_price_output` 欄位。
- `enforce_distillable_text`：Boolean；要求路由只使用允許文字蒸餾的模型。
- `http_referer`、`app_title`：只在設定明確提供時送出；`http_referer` 必須是 HTTPS URL。
- `allow_private_http`：只供受控開發用途。公開 HTTP 永遠拒絕；正式 OpenRouter 應使用 HTTPS。

未知欄位會被 API 以 `SET-003` 拒絕，不能靠前端繞過。Provider kind 也由 server allowlist 限制為 `openai`、`openrouter`、`openai_compatible`、`ollama`。

若既有 `kind=openai_compatible` 的 Base URL host 是 `openrouter.ai` 或其正式子網域，Migration 33 會在同一交易中保留 options、補上 `require_parameters=true`、停用 Batch 並持久化為 `kind=openrouter`；API 與 worker 也會在 migration 前以相同 effective kind 解讀，避免繞過 routing／privacy contract。相似但不是真正 `openrouter.ai` 的 suffix 不會被接受。

## 實際 request contract

OpenRouter request 與一般 OpenAI 相容 Provider 的差異集中在 adapter：

```json
{
  "model": "openai/<完整模型 ID>",
  "messages": "同一次 request 內含繁中 system prompt、文字指示與一張 data:image/jpeg;base64 圖片",
  "response_format": "支援且已勾選嚴格 Schema 時才送出",
  "provider": "options 正規化後的 routing object；沒有 routing 選項時不送空 object",
  "usage": {"include": true},
  "reasoning": {"effort": "low"},
  "session_id": "僅在 session_sticky=true 時，以 provider、workflow/photo、Vision fingerprint 與 reservation owner context 雜湊產生"
}
```

目前 OpenRouter 的推理欄位使用 `reasoning: {"effort": ...}`；InkTime 的 legacy `max` 會轉成 OpenRouter 公開欄位使用的 `xhigh`，不會把 OpenAI 專用的 `reasoning_effort` 原樣送給 OpenRouter。可參考 [OpenRouter reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens) 與 [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)。

### JSON repair 也必須遵守同一份 policy

Vision 回應若只需要一次 JSON schema repair，repair request 仍會送出上一個模型產出的文字內容，因此不能遺失 privacy／routing contract。Vision 與 repair 共用同一個 adapter helper：`provider` 只取 `OPENROUTER_ROUTING_KEYS` allowlist，保留 `require_parameters`、`allow_fallbacks`、`data_collection`、`zdr`、`usage.include=true` 與啟用時的 `session_id`。Repair 是 text-only，沒有 `image_url`、不重新讀圖片、不重新觸發 Vision，也不送 `reasoning`；OpenRouter 的 `max` 仍只在 Vision request 轉為 `xhigh`。

若設定了 `http_referer`／`app_title`，adapter 會送出 OpenRouter 文件所需的 `HTTP-Referer`／`X-Title` headers；不設定就不猜測、不填入 placeholder。

## 成本與 usage

InkTime 依下列優先順序保存成本：

1. Provider response `usage.cost` 或 response top-level `cost` → `cost_source=provider_reported`。
2. 已在 Provider 模型價格表填入的 Token 估算 → `cost_source=estimated`。
3. 兩者都沒有 → `cost_source=unknown`，不當作零。

同時保存 `input_tokens`、`output_tokens`、`cached_tokens`、`cache_write_tokens`、`reasoning_tokens`、request ID、prompt／schema 字元數、request body bytes 與 image bytes。成本頁、照片詳情與預算計算會把 unknown 單獨標示；有可計費證據的 unknown 會按 `budget.unknown_request_reserve` 計入每日／每月／照片／工作有效成本，同一照片或工作仍會阻擋重試。補齊該 Provider／模型的 input、cached input、output（以及 cache-write 尚無定價欄位時的未知狀態）後，管理員儲存價格會回溯 reconciliation；未知不會被當作零。

OpenRouter 的本地 AI Cache 仍是 InkTime 自己的內容／Provider／模型／Prompt／Schema／Vision fingerprint；不要把 OpenRouter 的 upstream response cache 當成 InkTime cache，也不要因 `session_sticky` 改變圖片 request identity。

## 隱私與資料邊界

- `never_upload`、本機預篩選、SHA duplicate 與 cache hit 發生在 Provider 呼叫前；被擋下的照片不會送 OpenRouter。
- `data_collection=deny`、`zdr=true` 是 routing policy 輸入，不是對供應商資料保留政策的替代品；仍須閱讀所選模型與實際 provider 的條款。
- Token、Base URL userinfo、Authorization、Base64 圖片與本機路徑都不應進入 Log；診斷輸出使用既有遮罩規則。
- OpenRouter request 目前只接受 server 產生的固定 Schema 與 stage prompt；管理員可調整的 caption generation controls 會納入 Prompt version，顯示 style 只在本地選取候選，不會重新上傳圖片。

## 驗收順序

1. 先儲存 Provider，確認 `openrouter` type、HTTPS Base URL、options allowlist 與 Batch 未勾選。
2. 先按「Level 1／連線」；它只做設定／`/models` capability check，不送圖片，也不自動產生付費 Vision request。
3. 需要確認 image input 時，再按「Vision Level 2」；這會使用 256px deterministic synthetic image，要求短 JSON 的 `vision_ok`／`detected_shapes`，最多一次 Vision、`max_tokens<=256`、不做 repair，按鈕會先確認可能費用。
4. 需要完整 Schema、usage 與 cost contract 時，再按「Full Contract Level 3」；最多一次 Vision 加一次 text-only repair，不讀 production photo。結果以 `request_counting_policy=conservative_attempted_calls` 回報 `repair_attempts`／`repair_responses`／`network_request_attempts`／`network_responses`；repair timeout、HTTP 500 或 connection reset 仍算 attempted call，沒有 repair usage 時成本為 `unknown`。
5. 在成本頁確認是 `provider_reported`、`estimated` 或 `unknown`，不要只看顯示的美元數字；真實 OpenRouter 與付費行為仍需另行核准。

本分支的 hosted CI 只驗證 request contract、Fake／offline 路徑與安全邊界；真實 OpenRouter API、付費請求、家庭照片與正式資料保留驗證維持 `NOT RUN`，需由人工依正式環境核准後另行執行。
