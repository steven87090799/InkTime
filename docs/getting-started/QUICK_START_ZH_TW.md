# 快速開始

1. **先選部署方式**：正式 NAS 依[NAS Tag 指南](../operations/NAS_TAG_DEPLOYMENT_ZH_TW.md)準備 `.env.nas` 與實際路徑，以已發布 Tag 執行 `sudo ./scripts/update_nas.sh --initialize vX.Y.Z`；日後更新省略 `--initialize`。NAS 不做本機 Build。
2. **本機開發／模擬**才使用 `.env.local.example`，並同時指定 `docker-compose.yml`、`docker-compose.dev.yml`。實際命令與 LAN／HTTPS 邊界見[安裝指南](INSTALLATION_ZH_TW.md)。
3. 確認 Web、Worker、Scheduler 均運作，瀏覽 `/setup` 建立第一位管理員；先建立備份。
4. 到「維護」以容器內 `/photos` 掃描照片。新安裝預設 `local_only`，不需要模型 Key；可先用 `/simulator` 預覽，或以維護頁「掃描並送到虛擬墨水屏」配合 `/virtual-display`。
5. 要使用 AI 才到「設定」搜尋「分析執行模式」並選 `automatic_ai`，再到「模型與 API」設定 Provider、該 Provider 的完整模型 ID、Key 與價格。先做連線／合成圖片測試；圖片測試可能計費。
6. 以 1–3 張非敏感照片確認 Schema、文案與費用，再逐步建立小批 `single` 工作。從[AI Trace、Activity](../guides/ACTIVITY_AI_TRACE_ZH_TW.md)與成本比對，不能只看工作 `completed`。
7. Batch 是另外選用的功能，只給已確認支援完整 Files／Batches 生命週期的 Provider；OpenRouter 不支援此路徑。先完成[Batch 人工 smoke](../OPENAI_BATCH_ANALYSIS_ZH_TW.md)，再考慮 100 張 sample 與全庫。
8. 到「渲染」選擇版型、字型及與面板相符的 Profile，預覽後發布；新自製 ESP32 以實體配對碼核准，既有 Legacy Token／Stock 裝置依相容模式操作。
9. 在「裝置」核對設定 ACK、下載與真正顯示結果；虛擬畫面與 CI 都不取代實板驗收。

Intel N100 先維持 `analysis.concurrency=1`、`worker.queue_multiplier=1`；依部署環境的實際 RSS／延遲再調整。日常新照片由增量掃描納入，模型、策略與保留政策見[現行基線](../reference/CURRENT_STATE_ZH_TW.md)。
