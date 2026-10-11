# 韌體問題清單與 F01/F02 修復：2026-10-11

稽核基準：`7ef3e4f`；修復接續免配對碼版本 `47dcd51`，同一 PR #150 分支。使用者已批准 F01/F02，韌體原碼更新為 `2.8.8`；未本機編譯、刷機或操作實板，Hosted CI／實機驗收仍待完成。
「確認」是可由控制流程證明；「待驗收」不表示實板已發生故障。
涵蓋目前部署的 PhotoPainter Rev2.0／7C sketch、其設定／排程／ACK／Frame／Transport／電源 helpers，以及保留的 13.3 吋 sketch 與板型驅動入口。
這是此次稽核發現與尚未解除的問題全集，不是對未知硬體問題不存在的保證。

## 現行 PhotoPainter：確認缺口與待量測建議

| ID | 優先 | 問題與觸發 | 位置 | 建議與驗證 |
|---|---|---|---|---|
| F01 | P2／已修改，待驗證 | **空庫獨立設定下發只修到 latest 路徑。** 已處於 `inktime_offline_schedule` 的裝置呼叫離線排程；伺服器 `schedule_not_ready` 404 不附 `device_config`，韌體只保存 retry，只有明確 delivery-mode mismatch 才轉 latest。因此同模式下沒有可產生的照片排程時，新設定仍不能套用／ACK。 | `downloadOfflineScheduleAndFrames`、`applyRemoteDeviceConfiguration`；`device_offline_schedule` | 獨立且經認證的設定同步，或在明確 no-content 回應附設定；保留最後照片與既有正式排程，不能把所有 404 當成換模式。空庫、發布失敗與模式切換分別驗證。 |
| F02 | P2／已修改，待驗證 | **一次照片刷新失敗會覆寫最後成功照片紀錄。** `saveDisplayRecord(...,false)` 寫入同一個 `disp_record`；`restoreLastSuccessfulPhoto` 要求該紀錄 succeeded，GC 也只用成功紀錄保護 last-good SHA。A 成功、B 刷新失敗後，A 的恢復入口消失，快取也可能失去保護。 | `saveDisplayRecord`、`restoreLastSuccessfulPhoto`、`runFormalFrameGcForWake` | 將「面板目前是否可證明完整」與「最後可恢復成功照片」分開保存；失敗仍禁止 same-SHA skip，但保護 A。測失敗、NVS 寫入失敗、GC、電量頁與各斷電點。 |
| F03 | P3／待量測改進建議 | **NetworkBudget／最低刷新間距不是完整執行政策。** `networkBudget`、`nextConnectAction`、`shouldRefresh` 沒有 runtime caller；例如 Manifest 固定 30 秒，不使用電池策略的 15 秒。現有 12 秒 Wi-Fi、HTTP/TLS deadline 與 max-awake supervisor 仍存在，不能稱為無界重試。 | `power_policy.h:20`、`:31`、`:61`；`ink-display-7C-photo.ino:4091` | 先量測不同供電與失敗情境，再把適合的政策接到 caller；最低刷新間距不能直接套到配對／必要恢復並宣稱已省電。 |

## 2026-10-11 重新核對適用性

F01–F03 不依賴人工配對碼或網站核准，因此配對簡化本身不會使它們消失。
本次以最新 `origin/main=7ef3e4f` 核對，該 7C sketch 也與
`origin/fix/photopainter-boot-crash-20261009` 相同；沒有沿用附件舊版行號推論。

- F01 只限已使用離線排程、伺服器沒有可提供的正式排程時；正常有排程、latest 路徑，或新裝置從 claim 取得初始設定，不套用此結論。
- F02 只限新照片刷新失敗後的復原／GC 路徑；正常照片顯示不代表已觸發此問題。
- F03 降為待電流量測的改善方向。現有連線 timeout 與最大醒著時間仍有效，不能由未使用的 helpers 推定實機耗電異常，也不是必須修復的故障。
- `47dcd51` 的網站自動加入／數字密碼改動沒有更動韌體；後續按使用者批准，在該版本上追加以下 F01/F02 修復。免人工配對碼的流程保留。

## 已批准修復的內容與驗證邊界

- F01：`schedule_not_ready` 的已認證 404 在設定有效時附 `no_content=true` 與 schema 3 `device_config`；與 latest 共用設定組裝。韌體只在 JSON、target、no-content、版本／模式契約符合時套用，共用既有設定驗證與 NVS 保存流程；保存成功才更新記憶體設定，隨後由 status 回報 applied config version。儲存失敗保留舊版本，且沒有把空排程當成正式排程 ACK。未知 404、驗證失敗及舊伺服器仍走原恢復邊界。
- F01 不刷新面板或刪除快取檔案。設定版本更新後，舊正式排程仍須通過原版本一致性檢查；不能宣稱舊排程會繼續執行新設定。伺服器需準備符合新版本的正式排程。
- F02：失敗的刷新只保留 `disp_pending`，不寫入 `disp_record`；最後成功照片繼續供恢復與 GC 保護。成功刷新仍須紀錄寫入／readback 後才清除 marker。相同 SHA 的失敗仍禁止 skip。沒有改 NVS schema，也沒有增加獨立失敗紀錄或電源控制。
- 無法憑此修復找回舊韌體已覆寫／GC 刪除的照片；沒有可驗證的最後成功 Frame 時仍走既有恢復失敗路徑。
- Hosted 回歸：current／next 空排程的新設定、+05:45／08:17／旋轉／同步時間、未認證拒絕、GET 不提前 ACK、無照片 status 可 ACK 設定但不 ACK 排程，以及毀損設定的 bounded retry。C++ 測試擷取實際 sketch 函式、替換 NVS／面板 IO，覆蓋 A 成功 B 失敗、同 SHA 失敗、復原失敗、NVS 寫入／readback／marker 清除失敗、GC 保護與 legacy 紀錄。
- 測試案例尚待 Hosted 執行；實板斷電、真實 NVS／FFat、EPD 顯示與電流驗收仍列 V01–V06。沒有操作 GPIO／PMIC，也沒有刷機。

## 保留的 13.3 吋 sketch：尚未達現行產品契約

這不是目前 Hosted CI 的部署板型；優先度以「有人實際部署它」為成立條件。未證實目前相框執行此 sketch。

| ID | 條件優先 | 問題 | 位置與修復方向 |
|---|---|---|---|
| F04 | P1 | 配網 AP 使用固定公開密碼，`POST /save` 沒有 setup secret／nonce／嘗試次數限制；在 AP 開啟時附近第三方可改 Backend／Wi-Fi。 | `ink-display-133C-photo.ino:362`、`:488`。導入一次性 AP 憑證與配網授權契約。 |
| F05 | P1／P2 | 設定多個 NVS key 個別寫入，未檢查 begin／put／readback；失敗或斷電可能留下混合設定，仍回報成功並重啟。 | `ink-display-133C-photo.ino:211`、`:397`。先確認板型儲存，再採原子設定與失敗回復。 |
| F06 | P1／P2 | 下載沿用 static key URL 與預設 HTTP，沒有現行裝置 credential、版本化 Manifest、SHA／CRC 完整性驗證；固定長度只能防部分格式錯誤。 | `ink-display-133C-photo.ino:86`、`:700`、`:817`。先決定是否仍支持此原型，再移植經認證與完整性驗證的下載契約。 |
| F07 | P2 | 空白密碼不能清空；數字使用 `toInt` 再 clamp，非法字串變 0；只有整小時時區／排程，無法保留非整小時設定。 | `ink-display-133C-photo.ino:307`、`:362`。明確 keep／replace／clear 與嚴格輸入解析。 |
| F08 | P3／功能限制 | `rotate180` 可保存但畫面旋轉未實作；配網頁已明示此限制。 | `ink-display-133C-photo.ino:338`、`:817`。若繼續支持，實作旋轉或停止提供可操作選項。 |
| F09 | P1／P2 | Vendor driver `EPD_WaitUntilIdle`／`EPD_WaitUntilIdle_high` 只輪詢 BUSY，沒有 deadline；13C 初始化會呼叫它。面板斷線／BUSY 卡住時可能一直醒著，下載 deadline 無法保護此階段。 | `ink-display-133C-photo/epd.h:251`、`epd13in3.h:155`；sketch `:661`。導入板型正確的 BUSY timeout、錯誤回傳與 bounded sleep；目前 PhotoPainter 使用另一個驅動，不套用此結論。 |

## 仍需實機／長期驗收，不能由 CI 判定已修復

| ID | 項目 | 驗證入口與邊界 |
|---|---|---|
| V01 | 暫時配對／電量畫面、恢復照片的斷電一致性（原 R03） | 現行入口已有 `beginPanelMutation`／`disp_pending`；於標記前後、刷新中、恢復中斷電，驗證不錯誤 skip，且不導致每次醒來刷新。與 F02 分開。 |
| V02 | KEY1 短按／1.2 秒強制連網／4 秒維修（原 R07） | UI 已修正長按提示；仍需實板確認開機延遲、離線 local_next、ACK 遺失與模式切換的手勢。 |
| V03 | 配網 AP 名稱、密碼清空、特殊分鐘／時區（原 R09/R10/R12） | 現行 7C 原碼已修；核對實際廣播，08:03／08:17／+05:45 修改 Wi-Fi 後保持、NVS 失敗時不重啟。13C 未同樣修正，見 F04–F07。 |
| V04 | RTC 回撥、跨日／夏令時間、NTP 不通、CA 過期（原 U05） | 固定 UTC offset 是目前契約；離線遇到 DST 改變會保留舊 offset，須連網同步。驗證 slot 不重複、不形成短週期喚醒；不得以關閉 TLS 驗證修復。 |
| V05 | FFat/NVS 損毀、滿碟、ACK journal power loss／GC（原 U06/U07） | 原碼保留帶資料 mount failure；active／staged／in-flight／last-good references 有 GC 保護，F02 修復保留 last-good 來源，Hosted 回歸與實板驗收待完成。真實損毀／滿碟／斷電及長期 ACK 成長尚未量測。 |
| V06 | 耗電、heap／PSRAM 峰值、loop stack、NVS 次數（原 U09） | 舊開機 stack 修復與 hint 去重已合併；Hosted compile、短 sample 與 helper 存在不能證明長期電流／冷開機驗收。需不同供電、網路失敗、最大排程及多次刷新量測。 |

## 附件韌體項目的目前狀態

- R03：已有 marker 修補，但 V01 未實機驗收，F02 是額外恢復缺口。
- R07：伺服器／UI 提示已修；韌體保留離線短按，V02 待驗收。
- R09、R10、R12：現行 7C 已修；13C 對應缺口見 F04/F07。
- U02：legacy/latest 空庫回應已有設定；enhanced offline 空庫已追加 F01 設定下發／套用修復，待驗證。
- U05、U06、U07、U09：見 F03 與 V04–V06；沒有把待量測方向當成已發生故障。

F01/F02 已依使用者後續批准修復，待 Hosted CI 與實板驗收；13C 未修改。F03 先量測，再決定是否值得實作。
任何後續修改仍須保持 ALDO3 / Audio_VCC 供電、EPD ALDO4、GPIO0/5/21 與 recoverable app-only flash 邊界。
