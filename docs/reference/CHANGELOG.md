# 變更記錄

此檔的版本名稱與日期分別代表原始套件基線及後續主線更新；不以 ESP32 版本取代 Python 套件版本。


## 2026-08-31 主線文件同步（程式基準 `48d2b8d`）

- 同步 README、部署／使用／架構／韌體指南與文件索引，修正舊兩階段分析、ExifTool 及 13.3 吋正式 Profile 說明；未改執行程式。
- 補上 local-only 預設、AI 啟用流程、Activity／AI Trace／usage 判讀，保留歷史報告原測量日期與 CI 邊界。

## 2026-08-19 至 2026-08-31 已合併主線功能摘要

- NAS 改以 Git Tag 發布 GHCR，更新器加入路徑／marker／lock／deployment contract 3 與唯讀 recovery point 保護。
- PhotoPainter Rev2.0 TG28 ALDO4 修復；韌體目前 2.8.6，增加 KEY1 雙擊電源頁、停留後 SD 原圖恢復與 Portal timeout 保護。
- Legacy Web／Analyzer／Renderer 退休，保留舊 DB 表與現行 Device Token 相容路徑。
- 安全清理未引用 Photo Analysis 歷史；Activity 增量輪詢保留事件與工作控制更新。
- Provider 專屬模型欄位（Migration 52）、OpenRouter 完整模型 ID 驗證與 `reasoning.effort=none` 明確送出。
- 截圖／嚴重模糊選片門檻與容器 SQLite 安全更新。各變更的實際 CI／部署／實板狀態需依對應 source 查證，不由此摘要宣告 PASS。

## 2.0.0-dev（2026-07-17）

- 新增版本化 Migration、WAL、備份與完整性檢查。
- 新增登入、角色、CSRF、登入限制、加密 Secret 與裝置 Bearer Token。
- 新增持久化 Job、有界 Worker、暫停／續跑／取消／重試與重啟恢復。
- 新增本地指紋／品質特徵、縮圖快取、兩階段嚴格 Schema 分析、Batch 與 usage／成本。
- 新增主要繁體中文管理介面、錯誤中心、診斷、備份、渲染與裝置頁；進階批次編輯與還原流程列為後續項目。
- 新增裝置能源儀表板：400 天裝置自動回報的電池／電壓／刷新與完整喚醒時間歷史；不要求人工電流量測，也不以電源讀值阻擋刷新。
- 內建芫荽手寫風格與霞鶩文楷 TC 文青風格繁中字型，提供真實預覽、一鍵切換、驗證後原子上傳與逐段缺字阻擋。
- 新增 2bpp 原子發布、Manifest、SHA-256、字型覆蓋與新版 ESP32 下載流程。
- 新增 Docker 三服務、CI、安全／整合／效能與 E2E 驗證。
