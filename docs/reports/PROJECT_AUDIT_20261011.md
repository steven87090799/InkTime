# InkTime 全專案重新稽核與非韌體修復：2026-10-11

BASE_HEAD=`7ef3e4f`；分支：`fix/full-project-audit-20261011`。
附件以 `bca27aa` 為基準；PR #147 已合併，不能把附件中的問題直接當成最新主分支仍有的問題。
本次按子系統檢查入口、直接依賴、失敗／恢復契約與對應測試。未操作 NAS、付費 Provider、相框或使用者資料。
「全專案」指跨子系統重新稽核，不表示逐行形式化證明、實機驗收或無未知缺陷。

## 新確認且已修改的非韌體問題

| ID | 優先 | 原因與影響 | 修復／回歸 |
|---|---|---|---|
| N01 | P1／LAN 功能 | `base.html` 的共用操作鍵直接呼叫 `crypto.randomUUID()`；這個 API 限安全環境，實際 LAN HTTP 可能沒有該方法。建立工作、付費診斷／發布在送出前就拋例外。sessionStorage 存取被拒也會阻止操作。 | HTTPS 保留 native UUID，HTTP 用 `getRandomValues` 產生 UUID v4；同操作保留同鍵，完成才清除。storage 拒絕時保留同頁記憶體鍵。Hosted E2E 模擬缺少 randomUUID、重試／不同 payload、storage 拒絕及完成後新操作。storage 不可用時跨重載恢復仍無法保證。 |
| N02 | P2／安全 | 登入 `next` 只查開頭 `/`，未擋反斜線與控制字元；瀏覽器網址正規化可把 `/\host` 視為外部網址。已有共用驗證卻未使用；共用驗證另有 malformed IPv6 URL／Host 的未捕捉 ValueError。 | 登入共用 `safe_local_redirect_target`；不安全目標回 dashboard，合法 path／query 保留。URL 與 expected Host 解析錯誤均 fail closed。Hosted security regression 覆蓋這些輸入。 |
| N03 | P1／發布一致性 | `ReleaseCoordinator.reconcile` 未持發布共用鎖。發布已換 pointer、DB 尚未 commit 時，修復可讀到 staged／舊資料並把 pointer 改回舊 Release；發布隨後成功也無法補回它。兩次修復也共用同名 temporary pointer。 | snapshot 與修復提交持同一跨程序 metadata guard；大型 Payload hash 在鎖外。提交前重新核對完整 DB snapshot，若 publish／rollback／GC 改變狀態則 deferred，留到下一次維護。Hosted regression 分別暫停 activation/DB commit 與在 hash 中完成新發布，要求 repair 不覆寫新 pointer。 |
| N04 | P2／降級恢復 | Weather API 的 JSON root／current／daily 若是 list／字串，原碼 AttributeError 未捕捉，跳過 `refreshing=False` 與 notify；之後該 location 持續被視為刷新中。NaN／Infinity 溫度可被當成功；缺少日溫度曾被補成 0。 | 先驗證物件與非空日溫度陣列，拒絕非有限數值；無效回應走既有 stale／unavailable 與 retry，且捕捉整數 overflow。Hosted regression 驗證先成功、再 malformed、再成功，三次都能完成刷新且保留最後可用值。 |
| N05 | P2／還原錯誤 | 備份 manifest 為 null／list，或 files／file metadata／important_table_counts 型別錯誤時，AttributeError 可逃出驗證邊界。驗證器聲稱檢查格式卻無法以標準備份錯誤拒絕這些內容。 | 在任何 DB 交換前驗證結構，回 `BACKUP-002`；size 整數 overflow 同樣拒絕。Hosted fixtures 修改備份 manifest，要求既有照片／分析完全保留。保留既有 SHA 與筆數檢查、format v1/v2 相容及離線 runtime lock。 |
| N06 | P2／相依套件安全 | GitHub open Dependabot alerts #3/#4：兩份 Linux runtime lock 都釘選 Werkzeug 3.1.8，位於 GHSA-g6x2-hccm-hh4m 影響範圍。漏洞觸發條件是 Windows／NTFS 特殊 device name；未證實 Linux NAS 可被此方式攻擊。 | requirements 直接釘選修補版 3.1.9，amd64/arm64 lock 同步同一 universal wheel，SHA-256 取自 PyPI exact-version metadata。Python >=3.9 與 MarkupSafe >=2.1.1 契約滿足本專案既有 3.12／3.0.3。未執行本機安裝／build；Hosted hash-enforced build 與 security scan 待驗證。警示需合併後由 GitHub 重評，不自行 dismiss。 |

瀏覽器契約依據：[MDN randomUUID](https://developer.mozilla.org/en-US/docs/Web/API/Crypto/randomUUID)、[MDN getRandomValues](https://developer.mozilla.org/en-US/docs/Web/API/Crypto/getRandomValues)。
安全修補依據：[Werkzeug 官方 advisory](https://github.com/pallets/werkzeug/security/advisories/GHSA-g6x2-hccm-hh4m)、[3.1.9 release notes](https://werkzeug.palletsprojects.com/en/stable/changes/#version-3-1-9)、[PyPI 3.1.9 metadata](https://pypi.org/pypi/Werkzeug/3.1.9/json)。
這些是修復後的原碼行為與新增測試要求；尚未以本機 runtime 執行宣稱驗收成功。

## 子系統稽核覆蓋與結論

| 子系統 | 查核入口／契約 | 結論與限制 |
|---|---|---|
| 啟動／安全／使用者 | factory／platform、web access、auth repository、security、redirects、JSON scalars、preflight | 權限／CSRF／session version 入口保留；修 N02。LAN HTTP 是明確允許的降級模式，preflight 的 warning 不應被改成啟動不可用。 |
| AI 單張／路由／帳務 | analysis lifecycle、billable operations、budgets、provider request boundary、process isolation、model upload guard | R01/R02 有明確 not-sent 與 checkpoint／批准重送；未自動重送 ambiguous request。未知成本的預留不自行過期釋放。正式照片與 paid Provider 未實際呼叫。 |
| Batch | prepare／submit claim、upload/create 前 consent guard、poll／import／cleanup 身分核對、provider retry | 保留 side-effect unknown hold、frozen plan、最後送出前 privacy／cancel recheck；未把未知遠端請求當成可重送。真實供應商對帳仍是外部操作。 |
| 價格／預估／模型能力 | jobs estimate、selection-preview、provider pricing CAS、contract runner、providers/jobs UI | R04–R06 已有同模型價格、未知價格、不同 route 預估、隨機圖形測試；Token 是假設，Level 3 格式成功不冒充視覺成功。修 N01 的真實 HTTP 相容性。 |
| Worker／Scheduler | job repository claim／lease／cancel／dedupe、runner、process boundary、scheduler tick | 確認有界隔離／timeout／terminate／join／slot release、排程備份去重與掃描取消保護。真實 NAS 的長期 RSS／FD／WAL 未量測。 |
| 照片／掃描／預覽 | scanner census／walk errors、paths、formats／EXIF decode bounds、thumbnail shard lock、photo privacy route | 保留來源唯讀、尺寸／檔案上限、hash 同 FD、變更來源拒絕、取消不完成全量 missing census。未掃描使用者照片，也未改相片排序契約。 |
| 排序／選片／渲染 | scoring／content filter、local selection、rendering source／caption、font validation、bounded render cache | 保留 v4/v5 可讀、67/33 排名、人工保護、HEIF 方向與不描述畫面的短句契約。字型與圖片品質的代表樣本仍需產品驗收。 |
| Release／回滾／清理 | AtomicReleasePublisher validate／pointer／metadata guard、coordinator publish/reconcile/GC、rollback | 修 N03；GC 仍保護 active pointer、裝置 assignment、queue、offline schedule。整庫 hash reconciliation 的長期 I/O 成本仍需規模量測，不能宣稱已優化 NAS 性能。 |
| 配對／裝置／Queue | device auth、pairing transaction／nonce／credential version、latest／offline manifest、resilience ACK／retention | 不放寬裝置驗證或 release 授權；韌體所需的 F01/F02 暫不修改，包括與它們耦合的 backend 行為。 |
| 備份／Migration／設定 | backup format／offline replacement／safety snapshot／paid fence、released migration transaction、settings mutation／provider CAS | 修 N05；保留 schema downgrade 拒絕、離線 lock 與付費暫停 fence。恢復舊 DB 無法重建快照後的外部供應商帳單或遺失的原圖／Release payload。 |
| 通知／對外網路／觀測 | webhook DNS pinning／allowlist／timeout、stock transport bounds、offline alerts、diagnostic redaction、weather cache | SSRF 邊界與回應大小上限保留；修 N04。正常深睡不能由「無回報」直接當成故障；未對真實 Webhook／Stock endpoint 傳送資料。 |
| Web／操作恢復 | shared helpers、jobs／providers／rendering／backups busy/idempotency、API role boundaries、CSP | R08/R11 已有操作保護與管理員 critical 告警邊界；補 N01/N02。HTTP 替代 UUID 不降低隨機來源品質。 |
| 部署／CI／相依套件 | NAS Compose／update/restore CLI、container process health、dependency pins、CI event/route/full-ci | 不改 runtime，不部署。325 個 tracked Python 檔做 AST syntax；Hosted full suite 才負責功能、安全掃描、容器與板型編譯。沒有以依賴釘選或舊 CI 當成今日無漏洞保證。 |
| 韌體 | 現行 7C 入口／PMIC 安全契約、設定／ACK／display／offline／transport／storage／wake helpers；13C sketch／driver | 完整發現清單見 [FIRMWARE_REVIEW_20261011.md](FIRMWARE_REVIEW_20261011.md)：F01–F09 與 V01–V06。本次 firmware diff 必須為空。 |

## 附件逐項重新核對

- R01/R02：本次基準已含 PR #147 的 not-sent 清理、checkpoint 與明確批准新請求；保留不明請求的人工處理。
- R03/R07/R09/R10/R12：現行原碼已有舊修復；實機邊界與額外 firmware 缺口見獨立清單。
- R04/R05/R06：已有價格、路由預估與能力測試修復；沒有新增正常分析圖片／repair 回合。
- R08：已有 busy、operation key、診斷 checkpoint；本次補 HTTP／storage 相容性 N01。
- R11：公開登入不查詢／顯示詳細 critical 告警；管理員保留。
- U01：還原 fence 保留；外部對帳不能由 source inspection 自動完成。
- U02：latest 空庫路徑已修，enhanced offline 空庫仍有 F01。
- U03：送出前照片狀態／工作狀態複查已有接線；傳输開始後的資料與費用不宣稱可撤回。
- U04：原 CAS 與 publication compensation 鎖保留；本次補 background repair 共用鎖 N03。
- U05/U06/U07/U09：安全邊界／保留規則仍存在；實板、長期成長與耗電量測未完成。
- U08：保留既有 prompt／ranking 產品契約；沒有把未量測風格調整稱為 bug 修復。
- U10：沒有移除 process isolation；本次沒有執行本機 benchmark 或 NAS soak。

## 驗證與交付

本機允許範圍：`git diff --check`、325 個 tracked Python 檔的 AST syntax、修改的 JavaScript helper `node --check`、docs/navigation 與 dependency policy 靜態 validator。
實際結果：diff、JavaScript、Python 3.12 AST 與 navigation 均通過；完整 dependency policy script 因本機 Python 3.12 缺 `packaging` 未執行完成，未為此安裝套件。以標準函式庫核對兩份 runtime lock 的格式、hash 與全部 direct version 對齊通過；完整 policy 與套件相容性留給 Hosted CI。
新增 Hosted cases：登入安全輸入與合法返回、Release activation/DB commit 交錯及 hash 期間 snapshot 失效、malformed weather 恢復、malformed backup 不換 DB、UUID／storage browser 相容。
完整 Hosted CI 由 Draft PR 的 `full-ci` label 請求；推送後只查一次，不輪詢、不手動 dispatch／rerun。
queued／in_progress 須交付為 `CI_PENDING`，不能寫成測試全綠。未合併、未部署、未刷機。
