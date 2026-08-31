# Secret Recovery Bundle

一般 Web Metadata Backup 預設不含 Secret；NAS 更新器專用 recovery point 另有含 Secrets 的 DB／session key 保護流程，見[NAS 指南](NAS_TAG_DEPLOYMENT_ZH_TW.md)。需要搬遷或災難復原時，離線執行 `scripts/secret_recovery.py create bundle.json`，並以 Recovery Passphrase 加密 session key 與已加密的 Secret 資料列。

先以 `verify` 驗證 Bundle，再停止 Web、Worker、Scheduler 後執行 `restore`。還原會取得 exclusive runtime lock、先建立 session key 安全副本，且在密碼或 Checksum 不正確時不會寫入資料。Passphrase 不會儲存、記錄或顯示。
