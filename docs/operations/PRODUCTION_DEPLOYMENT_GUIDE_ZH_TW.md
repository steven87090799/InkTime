# InkTime 完整上線指南（NAS、Web、ESP32-S3）

本指南把 NAS 初次部署、Web 建立第一個 Release，以及 ESP32 配網、配對與顯示驗收串成一條操作流程。日後更新、故障復原及板型專屬命令，請使用各步驟連結的專項文件。

> 原始碼核對基準：2026-09-03，`51309e2`。資料庫 Migration 57、Vision Schema v4、韌體 2.8.6、Config Store payload v5、NAS deployment contract 3 是不同的版本契約。部署時以所選 Release 的程式碼與部署檔為準；版本不是使用者手動填入資料庫的設定。

## 1. 準備 NAS、儲存與網路

NAS 需要 Docker Engine 24+、Docker Compose v2、`realpath`、`flock` 與 Docker 操作權限。GHCR 映像支援 `linux/amd64`、`linux/arm64`；NAS 使用已發布的映像，不需要在 NAS 編譯專案。

| 服務 | 工作 | 共用資料 |
|---|---|---|
| `inktime-web` | 管理介面、登入、裝置 API、Release 下載 | `/data` 可寫、`/photos` 唯讀 |
| `inktime-worker` | 掃描、特徵、分析、選片與渲染 | 同一組 `/data`、`/photos` |
| `inktime-scheduler` | 排程、租約、備份、保留與通知 | 同一組 `/data`、`/photos` |

三個服務必須使用同一個映像。SQLite、`session.key`、備份與 Release 放在 NAS 本機可靠的 ext4／xfs／btrfs 資料目錄；原始照片放在另一個既有相簿目錄。

- 兩個路徑使用已存在、非 symlink 的 canonical 絕對路徑，不能相同或互為父子。
- `/photos` 與其下所有 nested mount 都必須唯讀。更新與復原不應寫入原始照片。
- 不把 SQLite 放到未提供可靠鎖定、同步與原子更名語意的遠端分享。
- 同機反向代理使用 loopback bind，再以 HTTPS 提供服務。管理員、瀏覽器與 ESP32 都必須能連到設定的公開 URL。

先建立專用資料目錄，讓容器 UID/GID `10001:10001` 可寫。以下路徑必須換成實際位置；`chown` 只用於這個新建的空資料目錄：

```bash
sudo mkdir -p /your/local/path/inktime/data
sudo chown 10001:10001 /your/local/path/inktime/data
```

若 NAS 使用 ACL，透過管理介面授予等效權限。既有資料、備份與 `session.key` 的權限問題請依[備份與復原文件](BACKUP_RESTORE_ZH_TW.md)處理，不要遞迴改整棵資料目錄的權限。

## 2. 取得部署檔與已發布版本

```bash
git clone https://github.com/steven87090799/InkTime.git
cd InkTime
```

選定已成功發布到 GHCR 的 `vX.Y.Z`，取得該 Release 對應的部署檔，再從其範例建立 `.env.nas`：

```bash
# 將 vX.Y.Z 換成實際已發布版本。
git switch --detach vX.Y.Z
cp .env.nas.example .env.nas
```

主機需要 `docker-compose.nas.yml`、`scripts/update_nas.sh`、`nas-deployment-contract.version` 與依同版 `.env.nas.example` 設定的 `.env.nas`。契約變更時一起同步，保留實際主機路徑與設定。不要直接以範例覆蓋已有的 `.env.nas`。

若 GHCR Package 是私有，先互動式登入；Token 只需讀取 Package，不能寫入文件或 `.env.nas`：

```bash
docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

版本發布條件、映像契約比對及維護者操作見 [NAS Tag 部署](NAS_TAG_DEPLOYMENT_ZH_TW.md)。版本不存在或 Publish workflow 未成功時，先完成發布再繼續。

## 3. 設定 HTTPS 或可信任 LAN

編輯 `.env.nas`，保留範例中的資源與 Log 設定，至少替換主機路徑與 URL：

```dotenv
INKTIME_IMAGE_REPOSITORY=ghcr.io/steven87090799/inktime
INKTIME_ALLOW_MUTABLE_IMAGE_TAG=0
TZ=Asia/Taipei
INKTIME_PORT=8765
INKTIME_BIND_ADDRESS=127.0.0.1
INKTIME_DATA_PATH=/your/local/path/inktime/data
INKTIME_PHOTO_PATH=/your/local/path/photos
INKTIME_PUBLIC_URL=https://inktime.example.com
INKTIME_COOKIE_SECURE=1
INKTIME_ALLOW_INSECURE_HTTP=0
INKTIME_PROXY_TRUST=1
INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE=0
```

`INKTIME_IMAGE_TAG` 由更新器依命令列注入。`latest` 是可移動別名，預設拒絕；本指南使用明確版本 Tag。

同機 HTTPS 反向代理對外監聽 443，轉送至 `127.0.0.1:8765`，正確設定 Host 與 Forwarded headers。`INKTIME_PROXY_TRUST` 必須等於實際可信任代理 hop 數；沒有代理時使用 0。不要公開 Docker socket、資料庫或 `/data` 目錄。

若只在可信任 LAN／IoT VLAN 直接使用 HTTP，將以下欄位一起替換，並使用實際 NAS 私有 IP：

```dotenv
INKTIME_PUBLIC_URL=http://192.168.1.100:8765
INKTIME_BIND_ADDRESS=192.168.1.100
INKTIME_COOKIE_SECURE=0
INKTIME_ALLOW_INSECURE_HTTP=1
INKTIME_PROXY_TRUST=0
```

這是明確降級的傳輸模式，不能公開到 Internet。PhotoPainter 目前預設支援嚴格 RFC1918 IPv4 的 HTTP 直連；其他板型依 [ESP32 指南](../devices/ESP32_GUIDE_ZH_TW.md)選用對應 LAN build。HTTPS 裝置仍需正確的 Root CA，不能跳過憑證驗證。

## 4. 首次初始化與健康檢查

先驗證 Compose 設定，不啟動容器；手動查詢 Compose 時也需提供所選版本供映像欄位展開：

```bash
INKTIME_IMAGE_TAG=vX.Y.Z docker compose --env-file .env.nas -f docker-compose.nas.yml config --quiet
```

首次部署必須建立 deployment-root marker；把版本換成第 2 步選定的已發布 Tag：

```bash
sudo ./scripts/update_nas.sh --initialize vX.Y.Z
```

`--initialize` 適用尚無 marker 的空資料目錄，或經管理者確認要納管、已有 `inktime.db` 的舊部署。看到 marker／路徑錯誤時，先核對資料位置，不要藉由刪除 marker 或建立替身空目錄繞過檢查。

更新器會驗證路徑、marker、鎖、Compose 實際設定與映像契約，再建立必要的 recovery point、重建服務並等待健康狀態。已有資料庫時必須能透過目前運行的 Web 容器建立一致 snapshot；失敗會在替換服務前停止。

```bash
INKTIME_IMAGE_TAG=vX.Y.Z docker compose --env-file .env.nas -f docker-compose.nas.yml ps
curl -fsS http://127.0.0.1:8765/health/ready
```

若 bind 使用 NAS LAN IP，上述健康檢查改用該 IP。HTTPS 部署再從瀏覽器所在網路檢查實際公開 URL 的 `/health/ready`。確認三個服務 healthy 後再操作 Web。

## 5. Web 首次使用：先完成一張照片

1. 開啟設定的公開 URL，進入 `/setup` 建立第一個管理員，然後登入。
2. 在設定中核對時區、照片路徑與執行模式。容器內相簿路徑使用 `/photos`。
3. 從維護／工作介面建立掃描工作，確認照片數量、EXIF、縮圖與本機品質結果。
4. 新安裝預設 `analysis.execution_mode=local_only`；可先用本機選片與渲染完成首張 Release，不需 Provider。
5. 若需要一般 AI 分析，在模型管理設定 Provider、模型與價格，再由管理員明確切換為 `automatic_ai`。先建立小批量 `single` 分析工作並設定預算；`local_with_manual_ai` 只允許明確的人工 AI 操作。
6. 查看工作、Activity 與 AI Trace 的狀態、使用量和時間；本機結果、快取或繼承分析不代表此次發出新 Provider 請求。
7. 在渲染頁選擇與裝置一致的面板 Profile、版型、字型及色盤，預覽後發布 Release。

目前 Vision 使用 Schema v4，必須提供 `visual_orientation`；semantic 與 local quality 分開排名，歷史 v1–v3 分析保留為 legacy，需重新分析才進 v4 semantic 排名。`smart_two_stage` 等舊策略名稱會正規化為 `single`，不應再當成可選的兩階段流程。內容過濾、人工恢復與排名規則見 [Vision v4](../VISION_SCHEMA_V4.md)。

完整 Web 操作見[管理員指南](../guides/ADMIN_GUIDE_ZH_TW.md)、[使用者指南](../guides/USER_GUIDE_ZH_TW.md)與[本機選片](../guides/LOCAL_ONLY_SELECTION_ZH_TW.md)。Provider Key 透過 Web 的加密設定保存，不寫入部署檔。

## 6. 選板、建置與燒錄

正式 sketch 位於 [`esp32/ink-display-7C-photo`](../../esp32/ink-display-7C-photo/README.md)。目錄名稱中的 7C 不代表所有支援板型都使用七色面板：

| 硬體 | Build profile | Server render profile |
|---|---|---|
| 既有 GDEY073D46 PCB | 預設板型 | `gdey073d46_7c` |
| 既有 GDEP073E01 PCB | 預設板型加 `INKTIME_PANEL_GDEP073E01=1` | `gdep073e01_6c` |
| Waveshare ESP32-S3-PhotoPainter | `DEVICE_PROFILE_WAVESHARE_PHOTOPAINTER` | `gdep073e01_6c` |

1. 先確認實板 revision、面板、USB 裝置身分與供電。
2. 依 [ESP32 指南](../devices/ESP32_GUIDE_ZH_TW.md)取得固定依賴、partition 與建置命令。PhotoPainter 另外依[專用指南](../devices/WAVESHARE_PHOTOPAINTER_ZH_TW.md)使用 16 MiB Flash、8 MiB OPI PSRAM 與專用 partition table。
3. 操作 PhotoPainter Rev2.0 前閱讀 [TG28 實板交接](../devices/PHOTOPAINTER_REV2_TG28_HARDWARE_HANDOFF_ZH_TW.md)，先備份完整 Flash，驗證檔案並保留可恢復副本。完整備份可能含 Wi-Fi 與裝置憑證，必須限制存取且不提交 Git。
4. 區分完整合併映像與 app-only 映像；app-only 寫入 `0x10000`，不能寫到 `0x0`，也不應為更新而擦除 NVS。使用與建置完全一致的板型及燒錄設定。
5. 重啟後核對韌體版本、Flash／PSRAM 與狀態，再進行配網。USB Port 可能在 Bootloader 與應用程式之間改變，應重新確認裝置身分。

PhotoPainter Rev2.0 的 EPD 電源是 TG28 **ALDO4**。GPIO0 BOOT、GPIO4 KEY1、GPIO5 PWR、GPIO21 IRQ 各有既定用途；不得改用其他 PMIC 寫入或移除 BUSY／雜湊檢查來掩蓋顯示失敗。完整腳位、按鍵時序與寫入範圍以實板交接及當前原始碼為準。

一般開發依 [`AGENTS.md`](../../AGENTS.md)使用 Hosted CI 建置與驗證韌體。編譯通過不代表實體面板已通過驗收。

## 7. AP 配網與自動配對

1. 無有效 Wi-Fi 設定時，韌體建立 `InkTime-XXXXXX` AP。使用面板顯示的本次 AP session 隨機八位數密碼。
2. 連上 AP，開啟 `http://192.168.4.1/`，輸入 Wi-Fi 與 InkTime Server。PhotoPainter 可輸入 `192.168.1.100:8765`，韌體會補上 `http://`；HTTPS 與 Root CA 在進階設定。
3. 儲存並連上伺服器後，讀取實體面板的短效配對碼。
4. 在 Web「裝置」輸入該碼、核對裝置資料並核准。
5. 裝置透過 claim 取得 Device Secret，先存入 A/B NVS 再 confirm；confirm 成功後才建立並啟用正式裝置。
6. 在 Web 設定面板 Profile、時區、顯示時間、方向與啟停，等待裝置的設定版本 ACK。

既有 Legacy Bearer 裝置與 Stock PhotoPainter `/dataUP` 是不同的相容路徑。保留原廠 Stock 韌體時，使用[交付模式指南](../devices/PHOTOPAINTER_DELIVERY_MODES_ZH_TW.md)，不要套用自製 InkTime 韌體的配對步驟。

配對撤銷、repair 與憑證生命週期見[自動配對指南](../devices/ESP32_AUTOMATIC_PAIRING_ZH_TW.md)；憑證問題見 [TLS／配網指南](../devices/ESP32_TLS_PROVISIONING_ZH_TW.md)。Device Secret 不應出現在 URL、截圖或序列紀錄中。

## 8. 第一張畫面與排程驗收

| Render profile | Server payload | 長度 |
|---|---|---:|
| `safe_4c` | 480×800、2bpp | 96,000 bytes |
| `gdep073e01_6c` | 480×800、indexed4 | 192,000 bytes |
| `gdey073d46_7c` | 480×800、indexed4 | 192,000 bytes |

PhotoPainter 在 PSRAM 將驗證過的 server payload 轉為原生 800×480 六色畫面。Profile、尺寸、長度或 SHA-256 不符時保留舊畫面；不能強行把錯誤板型改成另一個 Profile 來跳過驗證。

依序核對：Web 裝置已啟用 → 期望設定版本與 ACK 一致 → 已取得 Release → 下載與 SHA-256 驗證成功 → 實際顯示完成 → Queue ACK／狀態更新 → 下一次排程喚醒。

另外記錄實體面板的方向、顏色、殘影、BUSY 時間及睡眠／喚醒表現。虛擬電子紙可以檢查交付協定，不能替代硬體驗收。遇到 BUSY timeout 時先保存錯誤與供電／接線證據，按[PhotoPainter 復原指南](../devices/PHOTOPAINTER_RECOVERY_ZH_TW.md)處理。

## 9. 備份、更新與復原

完成首次設定後建立備份，確認可還原並保留異機副本。一般備份與包含 Secret／`session.key` 的災難復原材料有不同用途，依[備份還原](BACKUP_RESTORE_ZH_TW.md)和 [Secret recovery](SECRET_RECOVERY_ZH_TW.md)保存。

後續更新使用下一個已發布版本，省略 `--initialize`：

```bash
sudo ./scripts/update_nas.sh vX.Y.Z
```

既有資料庫的更新會先建立 recovery point，包括一致的 SQLite snapshot、`session.key` 副本與版本 metadata；不包含原始照片，也不取代完整備份政策。不要直接以 `docker compose up` 繞過更新器的資料保護步驟。

只有確實搬移已納管的資料／照片根目錄，或明確接受對應的部署契約更新時，才在核對內容後依 [NAS Tag 部署](NAS_TAG_DEPLOYMENT_ZH_TW.md)使用 `--accept-path-change`。

| 問題 | 處理 |
|---|---|
| `NAS-UPDATE-MARKER-*` | 核對初次初始化、既有 marker、資料位置與搬移紀錄 |
| `NAS-UPDATE-CONTRACT-001` | 同步所選 Release 的部署檔與映像契約 |
| `NAS-UPDATE-TAG-001` | 使用已發布的明確版本 Tag |
| GHCR denied／manifest unknown | 核對 Package 登入、可見性、Tag 與 Publish 結果 |
| 容器 unhealthy／Migration 失敗 | 保存 Log、資料與 recovery point，依疑難排解處理 |
| 裝置 401／403 | 核對啟用、撤銷與憑證版本，必要時由管理員核准重新配對 |

更新失敗時不刪除 Volume、不執行 `down -v`。若資料庫已升級，不能只換舊映像硬降版；先停止服務，再按相容版本的備份還原程序處理。

## 10. 完成交接

- [ ] 三個 NAS 服務 healthy，實際公開 URL 可用。
- [ ] `/data` 可持久化、`/photos` 實際唯讀，NAS 重啟後仍可正常服務。
- [ ] 管理員、備份、掃描與首張 Release 已完成；AI 使用量與預算依實際需求驗證。
- [ ] 板型、韌體、配對、設定 ACK 與實體畫面一致。
- [ ] 已觀察下一次排程喚醒；畫面與功耗結果分別記錄。
- [ ] 未執行的 NAS／硬體／真實 Provider 驗證明確標示 `NOT RUN`。

更多錯誤碼與處理流程見[疑難排解](TROUBLESHOOTING_ZH_TW.md)及[驗收紀錄](../post-merge-hardware-validation.md)。
