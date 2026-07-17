# 模型與 API Provider 指南

支援 OpenAI 即時 API、OpenAI Batch、OpenAI 相容 API 與本地相容端點。新增時填入名稱、Base URL、API Key、優先順序、RPM、TPM、最大並行、逾時、冷卻時間與能力標籤。API Key 以 Fernet 加密，UI 只顯示遮罩。

Provider 必須回傳 OpenAI 相容 `choices[].message.content` 與 `usage`。模型輸出需符合嚴格 JSON Schema；不支援 Schema 的端點仍會在應用層驗證。失敗會依優先順序切換，連續失敗開啟熔斷；429 的 Retry-After 會延長冷卻。

Batch Provider 先上傳最多 50,000 筆、200 MB 的 JSONL（purpose=batch），再以 `input_file_id` 建立批次。100,000 張工作需拆成多個 Batch。目前已驗證提交、查詢與取消介面；背景 Job 的自動分批、結果匯入與中斷續跑尚未完成，因此正式大量工作目前仍走即時 API。測試請使用 Mock Provider，不使用真實付費 API。

價格需以每百萬 input、cached input、output Token 設定；未填價格時 Token 仍會記錄，但估計金額為零，管理員不得把它誤認為免費。
