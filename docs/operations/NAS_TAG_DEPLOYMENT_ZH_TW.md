# NAS 以 Git Tag 更新 InkTime Docker

從 NAS 初次部署、Web 首張 Release 到 ESP32 配對與顯示驗收，請先看[完整上線指南](PRODUCTION_DEPLOYMENT_GUIDE_ZH_TW.md)。

這條部署路徑讓 NAS 不需要每次複製原始碼，也不需要在低功耗主機重新 Build。維護者把已合併到 `main` 的版本建立 `vMAJOR.MINOR.PATCH` Git Tag 後，GitHub Actions 會建置並發布 GHCR 映像；NAS 只要指定同一個 Tag 執行更新工具。

```text
main 上的程式碼
  → git tag v1.2.3
  → GitHub Actions 建置 amd64／arm64 映像
  → ghcr.io/steven87090799/inktime:v1.2.3
  → NAS 執行 sudo ./scripts/update_nas.sh v1.2.3
```

映像只包含程式。SQLite、Session Key、縮圖、備份與 Release 全部保留在 NAS 的 `/data`；原始相簿由另一個既有 host 目錄掛到 `/photos`。Compose 使用 long bind syntax、`create_host_path: false` 與 `read_only: true`，路徑不存在時必須失敗，不會悄悄建立空目錄。應用程式啟動時會從實際 mount 狀態確認 `/photos` 是精確的唯讀 mount；只改 YAML 文字但實際可寫會以 `DEPLOY-PHOTO-RO-001` 拒絕啟動，照片樹下任何可寫 nested mount 則會以 `DEPLOY-PHOTO-RO-002` 拒絕啟動。

## 1. 發布規則

工作流程 [`.github/workflows/publish-container.yml`](../../.github/workflows/publish-container.yml) 只接受：

- `v1.2.3`：穩定版，同時更新 `latest`。
- `v1.2.3-rc.1`：預發布版，不更新 `latest`。
- Tag 必須指向 `main` 歷史中的 Commit。
- Tag 指向的 exact Commit 必須已有成功的 `Repository gate` 與
  `Container security gate`；缺少、執行中或失敗都會在建置／push 映像前 fail closed。
- 已存在的版本 Tag 不可重新指向另一份映像；要修正內容請建立新版本。

每次發布會產生 `linux/amd64` 與 `linux/arm64` 映像、Commit SHA Tag、OCI metadata 與 provenance attestation。映像另帶 `io.inktime.nas-deployment-contract` 標籤；更新器會在 pull 後、重建容器前，與 Repository 內唯一的 `nas-deployment-contract.version` 比對，不相容就停止。目前 NAS fail-closed mount／recovery 邊界是 deployment contract `3`，不可與 contract `2` 部署檔靜默混用。Intel N100／一般 x86 NAS 使用 `amd64`；ARM NAS 使用 `arm64`，Docker 會自動選擇正確架構。

## 2. 維護者建立版本

先確認變更已合併、`origin/main` 是要發布的版本，而且 Hosted CI 已成功。不要對尚未合併的功能分支建立正式 Tag。

```bash
git fetch origin --prune
git switch main
git merge --ff-only origin/main
git tag -a v1.2.3 -m "InkTime v1.2.3"
git push origin v1.2.3
```

推送後到 GitHub Actions 的 `Publish InkTime Container Image` 查看結果。只有該工作顯示成功，GHCR Tag 才可供 NAS 拉取；Queued、In progress 或 Failed 都不算已發布。

第一次發布後，請在 GitHub Package 設定確認映像可見性：

- 公開 Package：NAS 可直接 `pull`。
- 私有 Package：NAS 必須先登入 GHCR；Token 只需 `read:packages`，不要寫進 `.env.nas` 或 Commit。

互動式登入可避免 Token 出現在命令列歷史：

```bash
docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

## 3. NAS 首次設定（只做一次）

NAS 需要 Docker Engine 24+、Compose v2、`realpath` 與 `flock`。最簡單的首次設定是取得本專案一次；之後的一般程式版本更新不需要在 NAS Build：

```bash
git clone https://github.com/steven87090799/InkTime.git
cd InkTime
cp .env.nas.example .env.nas
```

NAS 主機端實際依賴 `docker-compose.nas.yml`、`.env.nas`、`scripts/update_nas.sh` 與 `nas-deployment-contract.version`。這四份檔案必須來自同一版本；Release notes 若標示部署契約有變，應一起更新後再執行。一般 Python／Web 功能仍包含在映像內。

編輯 `.env.nas`，至少替換：

- `INKTIME_DATA_PATH`：本機 ext4／xfs／btrfs 上已存在、可寫、非 symlink 的 canonical 絕對路徑，供 SQLite 與 `/data` 使用。
- `INKTIME_PHOTO_PATH`：已存在、可讀、非 symlink 的 canonical NAS 相簿絕對路徑；Compose 固定以唯讀 `/photos` 掛載。
- `INKTIME_PUBLIC_URL`：實際 HTTPS Origin。
- `INKTIME_BIND_ADDRESS`：同機 Reverse Proxy 可保留 `127.0.0.1`；可信任 LAN 直連才改成 NAS 的 LAN IP。

建立資料目錄並讓容器的非 root UID/GID `10001:10001` 可寫：

```bash
sudo mkdir -p /your/local/path/inktime/data
sudo chown -R 10001:10001 /your/local/path/inktime/data
```

更新器需要操作 Docker，並且只對本次新建、已確認為空的 recovery 目錄執行 `chown 10001:10001`；請以 `sudo` 執行下列更新命令。它不會 chown／chmod 整棵 data tree、既有備份或 `session.key`。

資料與照片路徑不得相同，也不得互為父子目錄。更新器不會用寫入測試探測照片目錄，不會在相簿新增暫存檔；它只確認路徑可讀，容器則用實際唯讀 mount 保護照片。`/photos` 下若另有 bind／tmpfs 等 nested mount，每一個也都必須是唯讀；`/photos/archive` 可寫會拒絕啟動，名稱相近但不在照片樹下的 `/photos-archive` 不受此規則誤判。

持久狀態邊界如下：SQLite 主檔、WAL／SHM 與應用程式鎖在 `/data/inktime.db*`；`session.key`、備份、Settings／Secrets、使用者、裝置、排程、Queue／Jobs、照片索引與分析／留存 metadata 都在該 SQLite 與 `/data/backups`；Release 與 rendered output 在 `/data/releases`；cache 在 `/data/cache`。原始照片只在 `/photos`，不屬於 recovery archive，也不可被更新流程寫入。

若只在可信任 LAN 使用 HTTP，需把同一組 Transport 設定一起改成：

```dotenv
INKTIME_PUBLIC_URL=http://192.168.1.100:8765
INKTIME_BIND_ADDRESS=192.168.1.100
INKTIME_COOKIE_SECURE=0
INKTIME_ALLOW_INSECURE_HTTP=1
INKTIME_PROXY_TRUST=0
```

這是明確降級模式，只能放在可信任 LAN／IoT VLAN，不可公開到 Internet。

## 4. 第一次啟動與日後更新

第一次啟動必須明確建立 deployment-root marker：

```bash
sudo ./scripts/update_nas.sh --initialize v1.2.3
```

`--initialize` 只允許 marker 尚不存在的空資料目錄，或已有 `inktime.db`、經管理者確認要納管的舊部署。日後普通更新必須找到 `/data/.inktime-deployment-root` 且路徑完全一致：

```bash
sudo ./scripts/update_nas.sh v1.2.4
```

若確實搬移了已納管的 data root 或 photo root，先人工核對新目錄內容，再執行：

```bash
sudo ./scripts/update_nas.sh --accept-path-change v1.2.4
```

更新器依序執行：canonical path／拓樸／marker 驗證 → 取得 bounded host `flock` → Compose config 與 resolved identity 核對 → pull → OCI deployment contract 比對 → recovery point → `--no-build` 重建並等待健康檢查。任何前置條件失敗都不會重建既有容器。

`.env.nas` 與命令列指定的 release Tag 是部署唯一來源。更新器會在隔離的 Compose 子程序中移除父 shell 繼承的所有 `INKTIME_*`，再明確注入本次 Tag；即使操作者的 shell 曾 export 另一組 data、photos、repository 或 `latest`，也不能覆寫已驗證的部署。重建前還會核對 Compose resolved environment 與 image identity，確保 updater 驗證、Compose 模型及執行容器使用同一組 `/data`、`/photos` 與 repository:tag。

`latest` 是可移動別名，預設拒絕。只有已接受不可重現風險時，才在 `.env.nas` 明確設定後使用：

```bash
INKTIME_ALLOW_MUTABLE_IMAGE_TAG=1
sudo ./scripts/update_nas.sh latest
```

不要以手動 `docker compose up` 取代更新器；那會繞過 marker、鎖、契約檢查與 recovery point。

## 5. 更新前後檢查

每次已有資料庫的更新，host updater 都會先在 `/data/backups` 新建一個空白、非 symlink、權限受限的 `update-recovery-...` 目錄。更新器要求目前的 `inktime-web` 容器仍在執行，並由該既有容器用 SQLite online backup API 對 live WAL database 建立一致的 staged snapshot；若沒有可用的現行 Web 容器或 snapshot 失敗，就在替換服務前 fail closed。目標映像的 sandbox recovery container 只會看到 production data root 的 `/source:ro`，以及該單一新目錄的 `/recovery:rw`；它沒有網路、capability、額外 NAS 路徑或整個 `/data` 的寫入權。

現行 Web 容器對 production DB 使用 SQLite `mode=ro`、`query_only` 與 online backup API，只把一致 snapshot 寫入本次新建的 bounded recovery 目錄。目標映像的 Recovery 工具會再次以唯讀模式驗證 live DB 身分與 staged snapshot 完整性，再讓既有 `BackupService` 只處理該 recovery 副本。完成後保留含 Secrets 的可驗證資料庫備份、權限 `0600` 的 `session.key` 副本，以及前版／目標映像、digest、Schema、deployment contract 與 SHA-256 metadata；不複製原始照片、快取、整個 Release payload。若唯讀 snapshot、mount、Session Key 或 archive 驗證任一步失敗，更新立即中止且不替換既有服務；目標映像沒有 production `/data:rw` fallback。這是更新前 recovery point，不取代異機備份政策。

更新後確認：

```bash
docker compose --env-file .env.nas -f docker-compose.nas.yml ps
curl -fsS http://127.0.0.1:8765/health/ready
```

三個服務都應為 `healthy`。診斷頁顯示的 Git revision 應等於 GitHub Tag 指向的 Commit；容器映像可另外用 `docker compose images` 查核。

## 6. 固定版本與回復

健康檢查失敗時，更新器不刪除 `/data` 或 recovery point，也不自動做可能不相容的降版。先保留診斷資料。若新版尚未執行不可逆資料 Migration，可在確認 Schema 相容後用上一個 Tag 重建：

```bash
sudo ./scripts/update_nas.sh v1.2.2
```

若已發生 Schema 變更，不可只換舊映像硬降版。請先停止三個服務，再依[備份還原指南](BACKUP_RESTORE_ZH_TW.md)把資料庫與 Release 一起還原。`latest` 會隨下一個穩定版移動，因此正式環境若重視可重現性，應記錄並使用明確的 `vX.Y.Z`。

## 7. 常見失敗

| 現象 | 原因與處理 |
|---|---|
| `denied`／`unauthorized` | GHCR Package 是私有；先以具 `read:packages` 的帳號登入。 |
| `manifest unknown` | GitHub Actions 尚未成功，或輸入不存在的 Tag；先查發布工作。 |
| `no matching manifest` | NAS 架構不是 `amd64`／`arm64`；此映像不支援其他架構。 |
| `NAS-UPDATE-004` | `.env.nas` 仍有 `/CHANGE_ME` 或範例網域，替換後再執行。 |
| `NAS-UPDATE-PATH-*` | 路徑不存在、不是 canonical、使用 symlink、不可讀／寫，或 data/photos 相同或巢狀；修正 host 目錄，不要建立替身空目錄。 |
| `NAS-UPDATE-MARKER-*` | 首次未用 `--initialize`、marker 損壞或路徑改變；先人工確認 root，只有真實搬移才使用 `--accept-path-change`。 |
| `NAS-UPDATE-CONTRACT-001`／`NAS-UPDATE-MARKER-006` | 映像、本機部署檔或既有 marker 契約不同；同步同一 Release 的四份部署檔，人工核對後以 `--accept-path-change` 接受契約更新，或改用相容 Tag。 |
| `NAS-UPDATE-IDENTITY-001` | Compose resolved data、photos、Tag 或 image identity 與 updater 已驗證值不同；檢查同版 `.env.nas`／Compose／updater，勿繞過更新器。 |
| `NAS-UPDATE-RECOVERY-*`／`NAS-RECOVERY-*` | 唯讀 SQLite snapshot、source／destination mount、Session Key 或 metadata 驗證失敗；既有容器尚未被替換，修正安全前置條件後再更新，不可改用 RW fallback。 |
| `NAS-UPDATE-LOCK-002` | 另一個更新程序持有相同 data root 的 lock；等待該程序結束，不要刪 lock file。 |
| `DEPLOY-PHOTO-RO-001` | 容器 mountinfo 沒有精確的唯讀 `/photos` mount；修正 Compose 與 runtime mount。 |
| `DEPLOY-PHOTO-RO-002` | `/photos` 下至少一個 nested mount 可寫；把照片樹下所有 mount 改為唯讀，或改用沒有可寫下層 mount 的 photo root。 |
| 容器 unhealthy | 先保留 Log 與資料，不要刪 Volume；執行 `docker compose ... logs --since=30m` 並檢查 `/data` 權限、URL／Cookie 組合與 SQLite filesystem。 |

更完整的資源、安全、Log、備份與資料庫限制見 [Docker 部署規格](DOCKER_GUIDE_ZH_TW.md)。
