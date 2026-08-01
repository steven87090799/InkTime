# 安全 OTA 設計（延後）

狀態：`deferred_with_reason`。

目前 PhotoPainter CI 使用 `app3M_fat9M_16MB`，本次尚未驗證雙 OTA slot、boot rollback、簽章金鑰生命週期、首次啟動健康確認與實體低電壓中斷。Bearer Token 不能代替韌體簽章，因此本 PR 不加入明文、未簽章或無回滾 OTA。

正式實作前必須同時具備：雙 OTA slot、可用 rollback、足夠 Flash、HTTPS 可信 CA、離線驗證簽章、board/minimum-version/size/SHA-256 manifest、防降版、低電壓禁止更新、寫入非目前 boot partition，以及首次開機未通過健康檢查時自動 rollback。以上需在實體 PhotoPainter 做斷電、損壞映像、錯誤簽章與低電壓測試後才能啟用。
