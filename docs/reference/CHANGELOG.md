# 變更記錄

## 2.0.0-dev 現行基準（2026-08-11）

- Database Schema 已到 Migration 50：自動配對、12／24 Slot 離線排程、穩定照片排序、單調裝置狀態、昂貴 POST fingerprint、完整照片庫 reservation lease／Idempotency ledger、nullable unknown cost、自動 API usage retention 與有界 cleanup audit GC。
- 正式分析策略收斂為 `local`／單次完整 `single`；預設 `analysis.execution_mode=local_only`，舊兩階段策略名稱只做相容正規化。
- Analysis Schema v3；Renderer 為 8 種版型、3 種 Profile、10 種抖動，並加入 canonical adaptive layout geometry 與 60 MP 輸入上限。
- ESP32 韌體 2.8.0／Config Store v5：possession pairing、24-slot 持久化容量、跨日 staged-next、crash-consistent ACK journal、權威 ACK identity 與單調 Status；v1–v4 仍以 legacy 12-slot 相容讀取。
- Provider／Worker／Release／Retention failure boundary、request-level 冪等、Queue／LKG referential protection、API usage 400 天生命週期與 90 天 cleanup audit GC 已補強。
- CI 使用 source-owned impact planner、source-head provenance、PR merge-ref validation、fail-closed aggregate attestation 與 CODEOWNERS；硬體／NAS／付費 Provider 驗證仍須分開標示。

## 2.0.0-dev 初始基線（2026-07-17）

- 新增版本化 Migration、WAL、備份與完整性檢查。
- 新增登入、角色、CSRF、登入限制、加密 Secret 與裝置 Bearer Token。
- 新增持久化 Job、有界 Worker、暫停／續跑／取消／重試與重啟恢復。
- 新增本地指紋／品質特徵、縮圖快取、當時的兩階段嚴格 Schema 分析、Batch 與 usage／成本；目前已由上方單次完整 `single` 契約取代。
- 新增主要繁體中文管理介面、錯誤中心、診斷、備份、渲染與裝置頁；進階批次編輯與還原流程列為後續項目。
- 新增裝置能源儀表板：400 天電池／電壓／刷新歷史、待機與喚醒電流量測參數，以及放電趨勢／容量模型雙重續航估算。
- 內建芫荽手寫風格與霞鶩文楷 TC 文青風格繁中字型，提供真實預覽、一鍵切換、驗證後原子上傳與逐段缺字阻擋。
- 新增 2bpp 原子發布、Manifest、SHA-256、字型覆蓋與新版 ESP32 下載流程。
- 新增 Docker 三服務、CI、安全／整合／效能與 E2E 驗證。
