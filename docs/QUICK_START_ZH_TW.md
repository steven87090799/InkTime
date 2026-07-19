# 快速開始

1. `cp .env.example .env`，將照片目錄掛載為 `/photos:ro`。
2. `docker compose up -d --build`，開啟 `http://NAS-IP:8765/`。
3. 建立管理員；到「模型」新增 Provider 並測試連線。
4. 到「維護」以 `/photos` 建立掃描工作；到「工作」觀察完成狀態。
5. 建立「兩階段智慧分析」，先用 10～100 張與小額預算驗證。
6. 到「成本」核對 usage；再逐步增加照片數與並行數。
7. 到「渲染」預覽並選擇內建手寫／文青繁中字型後發布；到「裝置」配對 ESP32 Token。
8. 到「備份」建立第一份備份；到「診斷」下載遮蔽後診斷包。

Intel N100 請先維持 `analysis.concurrency=1`、`worker.queue_multiplier=1`；確認 100 張真實照片的 Worker 峰值 RSS 後再考慮並行 2。部署、Log 與 ESP32 細節分別見 [Docker 部署規格](DOCKER_GUIDE_ZH_TW.md)、[Log 指南](LOGGING_GUIDE_ZH_TW.md)與[ESP32 指南](ESP32_GUIDE_ZH_TW.md)。

模型測試不應直接從 100,000 張開始。先確認分類、成本、字型與裝置版本，再執行全量工作。
