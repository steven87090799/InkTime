# 快速開始

1. 可信任 LAN Production 執行 `cp .env.lan.production.example .env`；正式 HTTPS Reverse Proxy 執行 `cp .env.production.example .env`。`.env.local.example` 只供 development／模擬。
2. 填入實際 URL、不同的絕對 data/photos 路徑與 Git SHA；LAN 執行 `python scripts/production_preflight.py --mode lan --env-file .env`，HTTPS 使用 `--mode https`。
3. 執行 `scripts/build_release_image.sh` 與 `docker compose up -d`；LAN Health 顯示 production／trusted-lan-http／degraded，且不可公開至公網。
4. 建立管理員。新安裝預設 `analysis.execution_mode=local_only`，可先完全不設定 Provider；需要 AI 時才到「模型」新增 Provider、設定 API Key 與模型價格並測試連線，再明確切換 `local_with_manual_ai` 或 `automatic_ai`。
5. 少量即時分析使用單次完整 `single` 工作；需要 OpenAI Batch 時，再到「Batch 照片分析」先執行 100 張 Sample，確認 Schema v3、分數、Token、成本、JSONL、峰值 RSS 與遠端 File 清理，最後才執行 `all_eligible_missing_analysis`。
6. 無實體面板時，先把照片放進 `simulation_photos/`，到「維護」按「掃描並送到虛擬墨水屏」，另開 `/virtual-display` 接收；正式照片庫仍可用 `/photos` 建立一般掃描工作。
7. 每週排名使用已保存分析；新增或變更照片可用本機選片、單次 `single` 或增量 Batch，正式流程不建立 Stage Two。
8. 到「成本」核對 usage；再逐步增加照片數與並行數。
9. 到「渲染」預覽並選擇內建手寫／文青繁中字型後發布；到「裝置」查看自製 ESP32 的自動配對核准，既有 Legacy 裝置才使用相容 Token。
10. 到「備份」建立第一份備份；到「診斷」下載遮蔽後診斷包。

Intel N100 請先維持 `analysis.concurrency=1`、`worker.queue_multiplier=1`；確認 100 張真實照片的 Worker 峰值 RSS 後再考慮並行 2。部署、Log 與 ESP32 細節分別見 [Docker 部署規格](../operations/DOCKER_GUIDE_ZH_TW.md)、[Log 指南](../operations/LOGGING_GUIDE_ZH_TW.md)與[ESP32 指南](../devices/ESP32_GUIDE_ZH_TW.md)。

模型測試不應直接從 100,000 張開始。先確認分類、成本、字型與裝置版本，再執行全量工作。
