# Token 與成本指南

新安裝 `local_only` 不產生模型費用；一般 AI 工作必須明確啟用 `automatic_ai`。`single` 不代表供應商永遠只計一次費：新建重跑工作、允許的重試與文字修復可能增加帳務，應以 usage／Provider 對帳。

成本節省順序：掃描時排除影片／動畫 → SHA-256 相同內容繼承 → pHash 近似群組 → 本機截圖／明顯品質缺陷預篩選 → E6 六色適合度模擬 → 同一 plan／Vision fingerprint 的快取 → 一次圖片 Vision（Web 預設 1024px、可選 1600px）。主要分析一次輸出所有欄位，不再另傳圖片產生短文案。

本機預篩選預設採 `conservative`：截圖達門檻即可排除，一般照片需同時符合至少兩項模糊、低對比、極端曝光或低解析度訊號。排除結果以 `prefilter / local-prefilter` 保存，`api_usage` 不會新增紀錄，因此是 0 Token、0 API 成本；原檔不會刪除。若誤判，可先標記最愛再重新建立分析工作，或在「設定」降低敏感度／停用對應規則。

E6 預篩選同樣完全在本機執行：以正式六色色盤量測量化後對比、主體細節、膚色與文字／強邊緣保留。低於 `analysis.e6_min_score` 才排除，且畫面會把它標為「電子紙適合度」，不代表原始照片品質差。可停用 `analysis.e6_prefilter_enabled`；最愛照片永遠略過。舊照片補算 E6 指標也不會呼叫 Provider。

每次 response usage 寫入 provider、model、job、photo、request type、input/output/cached Token、成本、延遲、狀態與重試。JSON 修復只傳文字且最多一次。

建議先設定每日／每月停止值、工作預算與單張上限。工作預估是區間，不是帳單保證；模型價格、圖片 Token 算法、Batch 折扣與快取命中都會影響實際成本。成本接近警告值時先暫停工作，核對 Provider 控制台與 InkTime usage。

## 成本來源與 fail-closed 規則

每筆 AI usage 都保存 `cost_source`：

- `provider_reported`：Provider 回傳可解析、非負的實際成本；這是唯一可直接視為 Provider 回報成本的來源。
- `estimated`：Provider 沒有回報成本，但已設定完整 Input／Cached Input／Output 價格，依 Token 換算的估計值。
- `unknown`：Provider 沒有回報成本，且價格不完整、無法解析或不適用。畫面會明確顯示 unknown，不會把它改成 US$0。

預算、成本頁與照片詳情的已知金額只計 `provider_reported` 與 `estimated`；另列有可計費證據的 unknown 筆數。每筆 unknown 以 `budget.unknown_request_reserve` 計入有效預算；同一照片或工作已有 unknown 時，該 scope 的重試會停止，但不同照片／工作的請求仍可依有效日／月預算判斷。管理員補齊該 Provider／模型價格後，保存價格會回溯可計算列並回報剩餘 unknown。OpenRouter 的回報成本若不存在，仍依同一規則處理；不能因為使用 OpenRouter 就假設價格已知。

Model Benchmark 另以 `attempted_photos` 作為平均成本分母；`known_cost_total` 只累計已知 usage。任何 attempted call 的 cost unknown 都使 `cost_complete=false`，平均與每 1000 張成本回報 `null`，不以零填補未確認帳務。

Migration 33 不會依照完全為零的 token、prompt／schema／request／image bytes 與成本欄位，把 historical `unknown` row 改標為 `estimated=0`；這些欄位是在舊 request 後新增的，零值不等於免費。無 billable evidence 的 row 可由 budget policy 不計 billable reserve，但 provenance 仍維持 `unknown`；任何 evidence 仍等待 reconciliation，不可用零成本掩蓋。

## 請求大小、快取與 Token 上限

目前完整照片分析固定遵守下列上限：

| 請求 | 影像邊長 | 最大 completion tokens | 備註 |
|---|---:|---:|---|
| 完整分析 | 512／1024／1600 | 2048 | 一次輸出分數、分類、原因與顯示文案欄位 |
| 變體分析 | 512／1024／1600 | 3072 | 僅在明確指定變體時使用 |
| JSON 修復 | 無圖片 | 1200 | 最多一次，repair model 只接收文字 |

每次 request 另保存 `prompt_chars`、`schema_chars`、`request_body_bytes` 與 `image_bytes`，讓圖片尺寸、Prompt、Schema 與傳輸大小可被分開追蹤；這些指標不是 Provider billing 的替代品。

## OpenRouter 與 Batch

OpenRouter 使用正式 `kind=openrouter` Provider contract，routing/privacy options 會進入 request body，並可使用 `order`、`only`／`ignore`、`data_collection`、`zdr`、`sort` 與 `max_price` 等受控欄位。OpenRouter request 不走 InkTime 的 OpenAI Batch 路徑；Batch 只保留給明確支援 `/files`、`/batches`、結果／錯誤檔與刪除的 generic 相容 Provider。完整設定見 [OpenRouter 正式 Provider 與安全契約](../providers/OPENROUTER_ZH_TW.md)。

正式啟用前請先使用 [Model Benchmark 規格](../providers/MODEL_BENCHMARK_ZH_TW.md) 的離線預設模式建立可重現的 JSON／Markdown 報告，再由管理員依實際 Provider policy、價格與少量人工 sample 決定是否送出真實請求。
