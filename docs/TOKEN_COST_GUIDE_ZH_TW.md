# Token 與成本指南

成本節省順序：SHA-256 相同內容繼承 → pHash 近似群組 → 本地截圖／模糊／曝光初篩 → 512px 第一階段 → 規則門檻 → 1600px 第二階段。主要分析一次輸出所有欄位，不再另傳圖片產生短文案。

每次 response usage 寫入 provider、model、job、photo、request type、input/output/cached Token、成本、延遲、狀態與重試。JSON 修復只傳文字且最多一次。

建議先設定每日／每月停止值、工作預算與單張上限。工作預估是區間，不是帳單保證；模型價格、圖片 Token 算法、Batch 折扣與快取命中都會影響實際成本。成本接近警告值時先暫停工作，核對 Provider 控制台與 InkTime usage。
