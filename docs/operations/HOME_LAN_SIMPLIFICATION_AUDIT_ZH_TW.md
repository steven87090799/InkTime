# 家用內網流程簡化檢查（2026-10-09）

## 已確認的阻塞與修改

1. 配對請求五分鐘到期，而 pending 的持久化 backoff 會升到五分鐘、十五分鐘、
   一小時。核准後裝置可能根本來不及領取。已有 request 的 claim／confirm 改為
   一分鐘重試；沒有 request 的連線錯誤維持有界 backoff，避免失敗時持續耗電。
2. KEY1 喚醒後仍受 pending 的舊 retry deadline 限制，可能立即再睡。
   KEY1 可以立即領取既有 enrollment；升級後也不等待舊版的一小時 deadline。
   加入 KEY1 wake armed／failed／held 與實際 GPIO4 wake 紀錄，不能以 HTTP 請求
   的時間接近按鍵就認定按鍵硬體已驗收。
3. 未配對先進 automatic pairing，擋住後面的實體 recovery Wi-Fi 設定。
   PhotoPainter 明確長按 KEY1 recovery 現在先進設定服務；也不受 revoked retry
   deadline 阻擋。GPIO0 BOOT、GPIO5 PWR、GPIO21 IRQ 與 PMIC 行為不變。
4. 可信內網新版韌體不再要求抄碼：管理員直接核准，請求保留一天，裝置自動
   領取。模式由伺服器實際部署 origin 決定，不能由 firmware capability 單獨
   宣告。無新增部署開關、DB migration 或 NVS schema。
5. 使用者已按「發布照片」後再跳一次「確認發布照片」沒有額外資料保護作用，
   移除這個確認；仍保留原有按鈕執行中防重入、Idempotency-Key 與 Release 回滾。

## 逐流程檢查結果

| 流程與來源 | 判斷／處理 |
|---|---|
| 啟動／部署：`factory.py`、`bootstrap.py`、`runtime_config.py`、`core/preflight.py` | 使用既有 origin 與 proxy 設定自動選配對政策；保留資料目錄／SQLite 部署契約，不添加另一套家用設定檔。 |
| 裝置配對：`api/device_pairing.py`、`services/device_pairing.py`、`devices.html` | 內網去掉六位碼與五分鐘操作窗口；public／proxy／舊韌體維持嚴格流程。 |
| Wi-Fi／按鍵／睡眠：`.ino`、`photopainter_support.cpp`、`pairing_recovery_core.h`、`photopainter_wake_core.h` | 修正 recovery 順序、pending 重試與 KEY1 gating；保留 timer recovery、電池睡眠、最大醒著時間與電源安全限制。 |
| 登入／權限：`api/auth.py`、`domain/auth.py`、`repositories/auth.py`、`platform.py` | 保留帳密、管理員權限、CSRF、HttpOnly、session version 與公開服務 TLS；內網直核准仍必須登入，避免任何網頁可代替使用者核准。登入 session 時間已有單一既有設定，不另增 LAN 專屬設定。 |
| 匯入／本機分析：`workers/scanner.py`、`repositories/settings.py` | 預設 `local_only`，本機選片不用 API key，不要求 AI 分析；保留格式／像素／檔案大小檢查。已用一張本機照片完成真實匯入與橫向預覽。 |
| 圖片發布／交付：`api/rendering.py`、`services/rendering.py`、`services/release_coordinator.py`、`photopainter_core.h` | 移除發布的重複確認；保留原子發布、版本、SHA-256、精確長度與面板 Profile。這些防止壞圖與寫入失敗，不是配對門檻。 |
| 工作／排程：`services/jobs.py`、`repositories/jobs.py`、`workers/runner.py`、`workers/scheduler.py` | 保留 background worker、lease、失敗分類與有界重試，避免 Web 被慢工作卡住、同張圖重複處理或死循環；pending enrollment 不套一般故障的長 backoff。 |
| 對外 AI：`providers/openai_provider.py`、`services/analysis.py` | 保留成本預算與 ambiguous outcome 人工確認，避免不確定請求自動重送造成重複計費；不改成本保護、不發出付費測試。 |
| 備份／韌體／CI：PhotoPainter safety contract、production deployment guide、CI artifact manifest | 保留可還原備份、app-only 寫入、精確 source/artifact 身分與 Hosted CI；這些是避免資料或硬體損毀的必要工作。 |

## 內網／對外界線

- 內網模式限 explicit RFC1918 IPv4 origin，`proxy_trust=0`，且韌體宣告支援新版
  協定。HTTPS 內網也可直核准；HTTP 是否允許仍依原有部署政策。
- 公開網域／公開 IP／代理部署維持五分鐘實體碼、嘗試限制、管理員登入、CSRF、
  TLS 與 credential confirm。從內網切換公開 origin 時，未完成內網 enrollment 失效。
- 不可把仍宣告 private origin 的服務透過 port forwarding／tunnel 公開；程式無法
  從 private bind IP 推知路由器對外轉發。對外部署須設定真正 public origin。
- nonce 與 Device Secret 不顯示於管理頁／紀錄。SHA-256 與硬體安全限制沒有降低。

## 驗證與尚未驗收

Python syntax、diff whitespace、AI navigation validator 已通過。新增 Hosted CI
案例涵蓋 LAN 無碼核准、管理員／CSRF、24 小時 TTL、legacy/public 防降級、切換
public 作廢，以及 active enrollment 的一分鐘 retry。沒有執行本機測試套件。

截至本檢查，實機仍安裝先前的 `56661d4` stack 修正版；它已有 app-only flash
與獨立 verify_flash 成功證據，觀察到完整 HTTPS request／正常 deep sleep，
尚未安裝本次配對簡化版。實體 KEY1、`stevenzhang` 2.4 GHz 關聯、credential
confirm、照片下載／實際畫面方向、電池／冷啟動仍須分開驗收。不能以 CI 或預覽
成功代替這些結果。最新實機證據保存在私有 PhotoPainterRecovery 目錄。
