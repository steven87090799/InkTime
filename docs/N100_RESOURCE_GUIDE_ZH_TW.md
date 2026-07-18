# Intel N100 資源與低功耗說明

## 結論

InkTime 適合單台 Intel N100 長期執行。正式預設以「低待機喚醒、單張大圖不完整常駐記憶體、工作有界、Log 有節流」為主，不追求一次把四個 N100 核心吃滿。

## 已確認並修正的瓶頸

| 原風險 | 影響 | 修正 |
|---|---|---|
| Worker 每 2 秒輪詢且每次做租約回收 | 固定 CPU／SQLite 寫入與磁碟喚醒 | 閒置預設 15 秒；租約回收最多每 60 秒 |
| Worker 與 Scheduler 重複頻繁回收 | WAL／鎖競爭 | 降頻並保留啟動恢復 |
| Web 2 workers × 4 threads | 每程序重複載入 Flask、Pillow、舊相容層 | N100 預設 1 worker × 2 threads |
| 原始 24MP／48MP 完整展開後才算特徵 | 每並行槽數十到數百 MiB 尖峰 | SHA 串流；特徵先 decoder draft／thumbnail 到最多 512px |
| pHash 直接四層 Python 迴圈與重算 cos | 掃描 CPU 熱點 | 可分離 DCT＋預先計算 cosine |
| 模糊度建立最多約 65k Python 整數 list | 每並行槽額外記憶體 | Welford 串流變異數 |
| 100k 工作建立全部 Future | 記憶體線性增長風險 | `concurrency × queue_multiplier` 有界 Future |
| 三容器每次啟動都做 SQLite 備份 | 重複 I/O 與磁碟占用 | 僅存在待套用 Migration 時備份一次 |
| 診斷頁每次掃整個縮圖目錄 | 100k 檔案時 I/O 尖峰 | 目錄大小預設快取 300 秒 |
| Gunicorn access log 記錄所有 probe | 無用 Log 與磁碟寫入 | 預設關閉；應用事件獨立分級 |

## 建議 Web 設定

| 設定 | N100 待機值 | 要提高的時機 |
|---|---:|---|
| `analysis.concurrency` | 1 | 小圖、RAM ≥8 GiB、模型端允許時可試 2 |
| `worker.queue_multiplier` | 1 | 模型 I/O 等待多且 RSS 仍低時可試 2 |
| `worker.poll_seconds` | 15 | 要更低待機可設 30～60；要更快啟動可設 5 |
| `scheduler.poll_seconds` | 60 | 純家用可設 300 |
| `worker.progress_items` | 50 | 10 萬張可設 100～500 減少 Log |
| `worker.progress_seconds` | 300 | 長模型呼叫維持 300，避免看不到進度 |
| `system.log_level` | INFO | 穩定後要極低寫入可設 WARNING |

CPU-bound 的 pHash、模糊度與 Pillow 解碼不應盲目提高 Thread 數；I/O-bound 的模型 HTTP 可以從並行 1 小幅測到 2。一次只調一個值，至少觀察一個完整工作。

## 如何量測

```bash
docker stats --no-stream
docker compose ps
docker inspect inktime-inktime-worker-1 --format '{{.State.OOMKilled}} {{.RestartCount}}'
docker compose logs --since=10m | grep -E 'OOM|out of memory|JOB-002'
```

Web「診斷」會分開顯示主機 CPU／RAM、目前 Web 程序 RSS／CPU／threads，以及 Docker cgroup 的目前記憶體與上限。Worker 的精確 RSS 請以 `docker stats` 為準，不能用 Web 程序 RSS 代替。

待機驗收建議：沒有工作時連續觀察 10 分鐘；容器 CPU 大部分採樣應接近 0%，SQLite WAL 不應持續快速增長，Log 不應每幾秒出現健康檢查或輪詢行。

## N100 調校順序

1. 先維持 Docker 預設上限與 Web concurrency=1。
2. 用 100 張實際照片掃描，記錄 Worker 峰值 RSS、CPU、耗時與失敗。
3. 若 RSS <500 MiB 且需要更快，再試 concurrency=2；若模型限流或 RSS 明顯倍增，改回 1。
4. 若工作以模型網路等待為主，可保留 concurrency=2；若以本地 pHash 為主，N100 的提升不一定線性。
5. 若主機只有 4 GiB RAM，不要關閉所有 swap；可用小型 zram 防突發，但不應讓圖片工作長期 swap。
6. 縮圖、備份與 Docker Log 都設保留策略，避免低功耗主機最常見的「磁碟滿」故障。

Intel 官方列出 N100 為 4C/4T、最高 3.4 GHz、6 MB cache 與 6 W base power；容器限制是為了平滑尖峰與保留主機服務餘裕，而不是因為 N100 無法執行。[Intel 官方產品資料](https://www.intel.com/content/www/us/en/products/compare.html?productIds=88183%2C231803)
