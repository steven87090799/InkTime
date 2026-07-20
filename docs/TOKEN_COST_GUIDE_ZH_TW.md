# Token 與成本指南

成本節省順序：掃描時排除影片／動畫 → SHA-256 相同內容繼承 → pHash 近似群組 → 本機截圖／明顯品質缺陷預篩選 → E6 六色適合度模擬 → 512px 第一階段 → 規則門檻 → 1600px 第二階段。主要分析一次輸出所有欄位，不再另傳圖片產生短文案。

本機預篩選預設採 `conservative`：截圖達門檻即可排除，一般照片需同時符合至少兩項模糊、低對比、極端曝光或低解析度訊號。排除結果以 `prefilter / local-prefilter` 保存，`api_usage` 不會新增紀錄，因此是 0 Token、0 API 成本；原檔不會刪除。若誤判，可先標記最愛再重新建立分析工作，或在「設定」降低敏感度／停用對應規則。

E6 預篩選同樣完全在本機執行：以正式六色色盤量測量化後對比、主體細節、膚色與文字／強邊緣保留。低於 `analysis.e6_min_score` 才排除，且畫面會把它標為「電子紙適合度」，不代表原始照片品質差。可停用 `analysis.e6_prefilter_enabled`；最愛照片永遠略過。舊照片補算 E6 指標也不會呼叫 Provider。

每次 response usage 寫入 provider、model、job、photo、request type、input/output/cached Token、成本、延遲、狀態與重試。JSON 修復只傳文字且最多一次。

建議先設定每日／每月停止值、工作預算與單張上限。工作預估是區間，不是帳單保證；模型價格、圖片 Token 算法、Batch 折扣與快取命中都會影響實際成本。成本接近警告值時先暫停工作，核對 Provider 控制台與 InkTime usage。
