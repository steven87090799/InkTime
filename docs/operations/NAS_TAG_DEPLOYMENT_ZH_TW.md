# NAS 以 Git Tag 更新 InkTime Docker

這條部署路徑讓 NAS 不需要每次複製原始碼，也不需要在低功耗主機重新 Build。維護者把已合併到 `main` 的版本建立 `vMAJOR.MINOR.PATCH` Git Tag 後，GitHub Actions 會建置並發布 GHCR 映像；NAS 只要指定同一個 Tag 執行更新工具。

```text
main 上的程式碼
  → git tag v1.2.3
  → GitHub Actions 建置 amd64／arm64 映像
  → ghcr.io/steven87090799/inktime:v1.2.3
  → NAS 執行 ./scripts/update_nas.sh v1.2.3
```

映像只包含程式。SQLite、Session Key、縮圖、備份、Release 與相簿分別保留在 NAS 的 `/data`、`/photos` Volume；更新或重建容器不會把它們包進映像或覆蓋掉。

## 1. 發布規則

工作流程 [`.github/workflows/publish-container.yml`](../../.github/workflows/publish-container.yml) 只接受：

- `v1.2.3`：穩定版，同時更新 `latest`。
- `v1.2.3-rc.1`：預發布版，不更新 `latest`。
- Tag 必須指向 `main` 歷史中的 Commit。
- 已存在的版本 Tag 不可重新指向另一份映像；要修正內容請建立新版本。

每次發布會產生 `linux/amd64` 與 `linux/arm64` 映像、Commit SHA Tag、OCI metadata 與 provenance attestation。Intel N100／一般 x86 NAS 使用 `amd64`；ARM NAS 使用 `arm64`，Docker 會自動選擇正確架構。

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

NAS 需要 Docker Engine 24+ 與 Compose v2。最簡單的首次設定是取得本專案一次；之後的一般程式版本更新不需要 `git pull`，也不會在 NAS Build：

```bash
git clone https://github.com/steven87090799/InkTime.git
cd InkTime
cp .env.nas.example .env.nas
```

NAS 實際只依賴 `docker-compose.nas.yml`、`.env.nas` 與 `scripts/update_nas.sh`；不想保留完整原始碼時，可在公開 Repository 合併此功能後，用 `curl` 下載這三份部署檔一次。未來若 Release notes 明確標示 Compose／部署契約有變，才需要同步新版部署檔；一般 Python／Web 功能更新都包含在映像內。

編輯 `.env.nas`，至少替換：

- `INKTIME_DATA_PATH`：本機 ext4／xfs／btrfs 上的絕對路徑，供 SQLite 與 `/data` 使用。
- `INKTIME_PHOTO_PATH`：NAS 相簿絕對路徑；Compose 固定以唯讀 `/photos:ro` 掛載。
- `INKTIME_PUBLIC_URL`：實際 HTTPS Origin。
- `INKTIME_BIND_ADDRESS`：同機 Reverse Proxy 可保留 `127.0.0.1`；可信任 LAN 直連才改成 NAS 的 LAN IP。

建立資料目錄並讓容器的非 root UID/GID `10001:10001` 可寫：

```bash
sudo mkdir -p /your/local/path/inktime/data
sudo chown -R 10001:10001 /your/local/path/inktime/data
```

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

使用穩定版 `latest`：

```bash
./scripts/update_nas.sh latest
```

正式環境建議指定不可變版本，方便稽核與回復：

```bash
./scripts/update_nas.sh v1.2.3
```

工具依序驗證 `.env.nas`、拉取 GHCR 映像、以 `--no-build` 重建三個服務，並等待健康檢查。指定 Tag 只在本次命令覆蓋 `.env.nas` 的預設值，不會改寫 Secret 或路徑設定。主機重開後，Docker 會依已建立容器的映像與 `restart: unless-stopped` 自動恢復。

手動執行同一流程時，可用：

```bash
INKTIME_IMAGE_TAG=v1.2.3 docker compose \
  --env-file .env.nas \
  -f docker-compose.nas.yml \
  pull

INKTIME_IMAGE_TAG=v1.2.3 docker compose \
  --env-file .env.nas \
  -f docker-compose.nas.yml \
  up -d --no-build --remove-orphans --wait
```

## 5. 更新前後檢查

更新前先從 Web「備份」建立並下載備份。更新後確認：

```bash
docker compose --env-file .env.nas -f docker-compose.nas.yml ps
curl -fsS http://127.0.0.1:8765/health/ready
```

三個服務都應為 `healthy`。診斷頁顯示的 Git revision 應等於 GitHub Tag 指向的 Commit；容器映像可另外用 `docker compose images` 查核。

## 6. 固定版本與回復

若新版尚未執行不可逆資料 Migration，可用上一個 Tag 重建：

```bash
./scripts/update_nas.sh v1.2.2
```

若已發生 Schema 變更，不可只換舊映像硬降版。請先停止三個服務，再依[備份還原指南](BACKUP_RESTORE_ZH_TW.md)把資料庫與 Release 一起還原。`latest` 會隨下一個穩定版移動，因此正式環境若重視可重現性，應記錄並使用明確的 `vX.Y.Z`。

## 7. 常見失敗

| 現象 | 原因與處理 |
|---|---|
| `denied`／`unauthorized` | GHCR Package 是私有；先以具 `read:packages` 的帳號登入。 |
| `manifest unknown` | GitHub Actions 尚未成功，或輸入不存在的 Tag；先查發布工作。 |
| `no matching manifest` | NAS 架構不是 `amd64`／`arm64`；此映像不支援其他架構。 |
| `NAS-UPDATE-004` | `.env.nas` 仍有 `/CHANGE_ME` 或範例網域，替換後再執行。 |
| 容器 unhealthy | 先保留 Log 與資料，不要刪 Volume；執行 `docker compose ... logs --since=30m` 並檢查 `/data` 權限、URL／Cookie 組合與 SQLite filesystem。 |

更完整的資源、安全、Log、備份與資料庫限制見 [Docker 部署規格](DOCKER_GUIDE_ZH_TW.md)。
