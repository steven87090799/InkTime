"""User-facing explanations only; never change retry, authorization or API codes.

The same catalogue is serialized to the browser. Original provider payloads and
machine codes remain intact; raw diagnostic text is bounded and redacted.
"""
from __future__ import annotations

import re

from inktime.app.core.ai_trace_payloads import bounded_text


CATALOG: dict[str, dict[str, str]] = {}


def _register(codes: str, title: str, detail: str, action: str) -> None:
    for code in codes.split():
        if code in CATALOG:
            raise ValueError(f"Duplicate error explanation: {code}")
        CATALOG[code] = {"title": title, "detail": detail, "action": action}


_register("AUTH-001", "帳號或密碼驗證失敗", "目前無法登入，可能是帳密不符或登入嘗試過於頻繁。", "確認帳密；若已被暫時限制，請等待限制解除後再登入。")
_register("AUTH-002", "頁面的安全驗證已失效", "表單的安全驗證資料已過期或與目前登入狀態不符。", "重新整理頁面後再操作；若仍失敗，請重新登入。")
_register("AUTH-003", "請先登入", "你尚未登入，或原本的登入狀態已過期。", "重新登入後回到原頁面。")
_register("AUTH-004", "目前帳號沒有操作權限", "此功能只允許具備相應權限的帳號使用。", "請由管理員操作；不要重複送出相同請求。")
_register("AUTH_REQUIRED", "模型服務拒絕驗證", "模型服務無法確認 API 金鑰或帳號權限。", "到「模型服務」確認金鑰、帳號權限與模型存取資格，再執行連線測試。")
_register("VLM-001 AI-PROVIDER-TIMEOUT PROVIDER_TIMEOUT", "等待模型回覆逾時", "在設定的等待時間內沒有取得完整回覆；不代表照片本身損壞。", "查看模型呼叫紀錄確認是否已送出；避免連續重送，必要時調整逾時或更換模型。")
_register("VLM-002 BATCH-RATE-LIMITED AI-PROVIDER-RATE-LIMIT", "模型服務的使用頻率已達上限", "模型服務限制每分鐘或每日可處理的請求數。", "等待服務指定的冷卻時間或額度重置；降低並行數，或改用額度充足的模型。")
_register("VLM-003", "模型回覆不是可解析的資料", "模型回覆不是有效 JSON，可能被截斷或混入一般文字。", "查看呼叫紀錄中的結束原因；若修復後仍失敗，改用支援結構化輸出的模型。")
_register("VLM-004 ANALYSIS-SCHEMA-INVALID schema_invalid", "模型回覆的欄位格式不符合要求", "回覆可能有缺少欄位、錯誤型別或不允許的值，因此不能當作正式分析結果。", "查看下方欄位說明；確認模型支援結構化輸出，再重試失敗項目。")
_register("VLM-005 AI-PROVIDER-UNAVAILABLE", "目前沒有可用的模型端點", "模型端點可能忙碌、冷卻中、受使用限制，或無法支援本次請求。", "查看模型服務回覆；暫時不可用可稍後重試，持續失敗則改用相容的固定模型。")
_register("VLM-006", "模型服務沒有回傳完整有效的回應", "請求可能已被處理，但回應格式或內容不足以確認結果。", "先查看 AI 呼叫紀錄與服務端使用量；不要直接連續重送，以免重複處理或計費。")
_register("VLM-007", "模型服務請求失敗", "模型服務在傳輸或處理請求時回報錯誤。", "查看 HTTP 狀態與服務回覆，確認端點及模型；暫時故障可待冷卻後重試。")
_register("VLM-008", "沒有符合這個工作的可用模型設定", "模型可能未設定、已停用、已刪除，或與建立工作時保存的設定不一致。", "到「模型服務」啟用相容模型並完成連線測試；設定改變時請建立新工作。")
_register("VLM-009", "需要先確認分析範圍", "完整照片庫分析會建立多張照片的模型請求。", "先確認本次排入數量及費用估算，再送出工作。")
_register("VLM-AMBIGUOUS JOB-SHUTDOWN-AMBIGUOUS", "請求是否已完成尚未確認", "連線或程序中斷後，系統無法確認遠端是否已完成處理。", "先核對呼叫紀錄與服務端使用量；確認後再決定重送，避免重複費用。")
_register("BUDGET-001 BATCH-BUDGET-001", "這個工作的預算不足", "已確認的用量或本次整批估算超過工作允許的預算。", "確認用量後調整工作預算或縮小處理範圍；不要把未知費用填成零。")
_register("BUDGET-002", "已達到 API 成本安全上限", "每日、每月或單張照片的用量已達設定上限。", "到「API 用量與成本」核對用量，再調整預算或等待下一個週期。")
_register("BUDGET-003 HISTORICAL-UNKNOWN", "舊工作曾因無法確認費用而暫停", "這是舊版未知成本限制留下的紀錄，不代表目前仍使用當時的模型。", "核對目前模型與用量；新版不再僅因同一照片的歷史未知成本阻斷工作。")
_register("JOB-001", "目前工作狀態不允許這個操作", "工作可能已完成、取消、暫停，或提供的建立參數不合法。", "重新整理工作頁並核對狀態及表單，再選擇可用的操作。")
_register("JOB-002 STALE_RECOVERY LEASE_EXPIRED WORKER_CRASH", "背景工作的處理租約已失效", "原處理程序可能已中斷，系統需要確認並回收未完成項目。", "查看工作狀態與背景服務健康檢查；不要重複建立相同工作。")
_register("JOB-003 TRANSIENT TEMPORARY_IO_ERROR", "背景項目處理失敗", "處理程序回報異常，未能產生可確認的完成結果。", "查看此項目的具體原因及背景日誌；修正後重跑失敗項目。")
_register("JOB-004", "背景項目的執行時間超過上限", "系統停止或隔離逾時的處理，避免工作永久卡住。", "檢查模型延遲、照片大小及主機資源，再調整逾時或重試。")
_register("JOB-FINALIZER-001", "工作結果的最後儲存步驟失敗", "項目可能已經處理，但最後的寫入或狀態更新未完成。", "先核對模型紀錄、結果與資料庫狀態，再決定是否重跑。")
_register("QUEUE-NO-WORKER JOB-HEARTBEAT-STALE", "背景工作沒有持續回報進度", "有待處理的執行中工作，但尚未收到有效的工作心跳；這不等於 Worker 數量不足。", "查看工作頁及系統診斷，確認背景服務是否運作；已暫停的工作需先恢復。")
_register("NO_PHOTOS NO_CONTENT NO_ELIGIBLE_CANDIDATES", "目前沒有符合條件的照片可處理", "照片可能已排除、已處理、尚未完成掃描，或不符合本次篩選。", "核對照片庫數量、排除條件與選片範圍後再建立工作。")
_register("ANALYSIS_DISABLED ANALYSIS-DISABLED", "照片分析目前已停用", "系統設定禁止建立新的分析工作。", "到「系統設定」確認分析執行模式，再選擇本機或模型分析。")
_register("CONFIG_INVALID UNSUPPORTED_CONFIGURATION", "目前設定無法執行這個操作", "設定值、模型能力或工作建立時保存的設定不相容。", "查看具體欄位原因，修正設定後重新建立工作。")
_register("AI-CACHE-001", "等待相同照片的分析結果逾時", "另一個工作正在計算相同分析，尚未取得可沿用的結果。", "查看原工作是否仍在執行，完成後再試，避免重複送入模型。")
_register("ACTIVITY-001 AI-TRACE-001 DECISION-001 REVIEW-001 HISTORY-001", "篩選或查詢條件不合法", "分頁位置、日期、照片範圍或篩選值不符合要求。", "清除篩選或重新整理頁面，再輸入符合欄位提示的條件。")
_register("AI-TRACE-404 TRACE-404", "找不到這筆模型呼叫紀錄", "紀錄可能不存在，或已依資料保留規則清理。", "返回 AI 即時追蹤，使用照片或工作查找仍保留的紀錄。")
_register("AI-TRACE-002 USAGE-001 ANALYSIS-OUTCOME-001 ANALYSIS-PERSIST-FAILED", "分析紀錄未能完整儲存", "紀錄的識別資訊或結果狀態無法確認。", "保留工作資訊並查看資料庫與背景日誌；先核對遠端結果，避免重複呼叫。")
_register("DISK-CRITICAL DISK-WARNING", "資料磁碟的使用比例過高", "磁碟接近容量警戒線，可能影響資料庫、縮圖及發布檔寫入；不是單張照片的模型錯誤。", "到系統診斷查看實際可用容量，確認保留策略；備份後才清理確定不需要的資料。")
_register("SQLITE-INTEGRITY DB-INTEGRITY-001 DB-001 DB-002 DB-LOCK-001 DB-TX-001 SCAN-DB-001", "資料庫讀寫或完整性檢查失敗", "資料庫可能忙碌、權限不足、空間不足，或未通過完整性檢查。", "停止重複送出操作；查看系統診斷、資料目錄權限與磁碟，必要時由管理員從備份恢復。")
_register("MIGRATION-001 MIGRATION-002 MIGRATION-003 MIGRATION-004", "資料庫升級未完成或版本不相容", "目前程式與資料庫結構無法安全配合，不能繼續寫入。", "請管理員停止服務，核對程式版本與升級紀錄，依升級前備份進行離線恢復。")
_register("SCAN-001 JOB-SOURCE-MISSING PHOTO-ELIGIBILITY-001 PHOTO-ELIGIBILITY-005 REVIEW-404", "找不到或無法讀取原始照片", "照片檔案可能已移動、刪除，或照片來源沒有正確掛載。", "確認照片來源及讀取權限，再重新掃描；不要刪除已有分析結果。")
_register("SCAN-002 SCAN-IO-001 SCAN-IO-002", "照片來源無法完整掃描", "照片資料夾的讀取或走訪失敗，掃描結果可能不完整。", "確認 NAS／磁碟掛載與資料夾權限；修復後重新掃描，不要先把照片標成遺失。")
_register("SCAN-003 SCAN-004", "掃描方式或來源設定不合法", "提供的掃描模式或來源不在允許範圍內。", "在照片庫與維護頁重新選擇有效來源與掃描方式。")
_register("SCAN-MISSING-THRESHOLD SCAN-MISSING-002 SCAN-MISSING-003 SCAN-MISSING-004", "照片遺失清單需要重新確認", "遺失比例超過安全值、掃描證據不完整，或已有較新的掃描結果。", "先確認照片掛載與數量，只使用同一照片庫最新的完整掃描進行人工確認。")
_register("SCAN-PHOTO-001 SCAN-SIZE-001 IMG-001", "圖片無法讀取或解碼", "圖片格式、檔案大小或內容完整性未通過檢查。", "查看本次回報的限制，確認原圖能正常開啟，再重新掃描。")
_register("IMG-002", "畫面使用的字型無法顯示所需文字", "字型可能缺失、損壞、格式不合法，或沒有包含短句所需的中文字元。", "到「渲染」選取內建繁中字型，或上傳涵蓋所需字元的 TTF、OTF、TTC 字型。")
_register("IMG-005", "照片的禁止上傳設定不合法", "這個欄位只接受明確的開啟或關閉值，未套用本次修改。", "重新整理照片詳情，再操作禁止上傳開關。")
_register("IMG-003 PHOTO-ELIGIBILITY-006 PHOTO-ELIGIBILITY-007", "照片尚未具備完整分析資料", "缺少必要的本機特徵或正式模型分析，暫時不能用於這個操作。", "先完成照片掃描及預處理；需要模型結果時，再建立 Vision 分析工作。")
_register("IMG-004", "照片操作的參數或範圍不符合要求", "本次提交的照片、排除狀態或表單內容不適用此操作。", "重新整理照片頁並核對選取範圍及欄位提示後再試。")
_register("PHOTO-ELIGIBILITY-002 PHOTO-ELIGIBILITY-003 PHOTO-ELIGIBILITY-004", "這張照片目前不在可處理範圍", "照片可能已排除、被標示遺失，或所屬照片庫已停用。", "到照片詳情核對狀態；確認需要後由管理員恢復或覆寫排除。")
_register("THUMB-001 THUMB-002 THUMB-003 THUMB-004 THUMB-005 REVIEW-422", "照片縮圖建立失敗", "原圖格式、尺寸或內容驗證失敗，也可能在處理期間被修改。", "確認原圖可讀且未持續變動，再重新掃描或重建縮圖。")
_register("PATH-001 SESSION-001 DEPLOY-PATH-OVERLAP-001 DEPLOY-PHOTO-RO-001 DEPLOY-PHOTO-RO-002 REVIEW-400", "資料路徑不符合安全規則", "路徑可能越界、重疊、使用不安全連結，或照片掛載並非唯讀。", "請管理員檢查資料與照片路徑及掛載設定；不要關閉安全檢查。")
_register("SEC-001", "無法解密已保存的敏感設定", "目前使用的主密鑰與保存資料不匹配，或加密資料已損壞。", "請管理員核對原主密鑰與備份；不要隨意重新生成密鑰覆蓋原資料。")
_register("RENDER-001", "無法建立要發布的畫面", "沒有可用照片，或本次發布數量／輸入不合法。", "確認已選取照片及合法數量，再重新產生預覽。")
_register("RENDER-002 DEVICE-QUEUE-HASH DEVICE-QUEUE-DOWNLOAD", "畫面檔案未通過完整性檢查", "檔案尺寸、長度、格式或校驗值與預期不一致。", "保留裝置舊畫面，檢查發布檔與網路代理，再重新產生並驗證畫面。")
_register("RENDER-003 DEVICE-CONFIG-PROFILE", "畫面規格與裝置面板不相容", "選擇的尺寸、色數、面板規格或設定版本與裝置不一致。", "核對實體面板型號、韌體及裝置設定，選取相容規格後重建畫面。")
_register("RENDER-004 RENDER-006 RENDER-007", "圖片渲染參數不合法", "色盤、抖動、縮放、預設樣式或原圖尺寸超過允許範圍。", "依具體欄位提示修正，或先恢復內建的相容渲染預設。")
_register("RENDER-005", "照片與版面配置無法配合", "版型、方向、裁切或短句與選定照片不一致。", "重新選擇照片和版型；雙照片版型需具備兩張相容照片。")
_register("RENDER-008", "背景渲染工作資訊不合法", "工作類型、識別資訊或結果不符合渲染流程要求。", "返回渲染頁重新建立預覽；若仍失敗，保留工作資訊供管理員查核。")
_register("RENDER-009", "同時建立的預覽或發布數量過多", "目前請求已超過並行或單次數量的保護上限。", "等待現有工作結束，或減少單次發布數量後再試。")
_register("RENDER-010 RENDER-RELEASE-STAGE RENDER-RELEASE-ACTIVATE", "發布資料不完整或狀態不一致", "發布清單、檔案、索引或識別資訊未通過檢查，不能安全啟用。", "保留目前可用畫面，查看發布紀錄與檔案完整性，再重新建立發布。")
_register("RENDER-011", "發布資料正在被其他工作更新", "等待發布索引的寫入鎖逾時，這次操作尚未完成。", "等待目前的發布工作結束後再試，避免同時重複發布。")
_register("RENDER-012 DEVICE-010 QUEUE-010", "這個發布版本的畫面檔已清理", "保留政策已移除實際畫面檔，因此不能再下載或回滾到此版本。", "選擇仍保留檔案的版本，或從原照片重新發布。")
_register("DISPLAY-001 DISPLAY-006", "換圖排程的設定或結果不完整", "裝置、日期、時刻、備援方式或發布結果不符合排程要求。", "確認裝置與換圖排程欄位，重新建立日期正確的排程。")
_register("DISPLAY-003", "排程需要的 AI 分析尚未完成", "目前排程要求先完成模型結果，因此暫時無法產生畫面。", "先處理照片分析工作，或明確調整排程允許的備援方式。")
_register("DISPLAY-004", "排程指定的裝置不存在或已停用", "找不到可以接收這次畫面的有效裝置。", "到裝置管理啟用正確裝置，再重新建立排程。")
_register("DISPLAY-005", "排程的畫面製作失敗", "系統未將此次排程標記為成功，並保留目前正式畫面。", "查看渲染工作原因，修正照片、版面或字型後再重試。")
_register("DISPLAY-CONFIG-RACE OFFLINE-002", "裝置或離線排程設定已改變", "目前工作保存的設定已過期，系統拒絕以舊結果覆寫新設定。", "重新整理裝置設定後建立新排程。")
_register("DEVICE-001 DEVICE-QUEUE-AUTH", "裝置身分驗證失敗", "裝置憑證、版本或啟用狀態與伺服器紀錄不符。", "到裝置管理確認啟用與配對狀態；必要時由管理員重新配對。")
_register("DEVICE-002", "裝置或發布檔案無法取得", "指定裝置可能已停用，或發布版本／畫面檔不存在。", "核對裝置與目前發布版本，再從裝置頁重新取得內容。")
_register("DEVICE-003 DEVICE-004 DEVICE-011", "裝置設定或狀態回報不合法", "參數、回報型別或認證／傳送模式不符合裝置要求。", "核對裝置頁設定與韌體相容性，依下方欄位說明修正。")
_register("DEVICE-006", "無法向這個裝置傳送測試畫面", "裝置可能不存在、停用，或測試畫面規格與傳送設定不相容。", "先在裝置管理確認裝置及面板，再選擇相容測試畫面。")
_register("DEVICE-007", "裝置的驗證請求過於頻繁", "系統暫時限制過多的驗證嘗試。", "先修正裝置憑證，等待限制解除後再連線，避免快速重試。")
_register("DEVICE-008 OFFLINE-001", "裝置離線排程或傳送模式不相容", "裝置能力、離線時刻、預取設定或發布數量不符合要求。", "確認韌體回報的能力，並核對離線時刻與裝置傳送模式。")
_register("DEVICE-009", "區域網路裝置傳送未完成", "裝置位址、網路連線、畫面完整性或傳送回覆驗證失敗。", "確認裝置位址與同一區域網路；若已上傳但沒有回覆，先查看裝置，避免重複傳送。")
_register("DEVICE-OFFLINE", "裝置已一段時間沒有連線", "伺服器超過設定時間未收到裝置連線或狀態回報。", "檢查電源、Wi-Fi、刷新週期及配對狀態；休眠裝置也需核對預定喚醒時間。")
_register("QUEUE-001 QUEUE-005 DEVICE-QUEUE-SCHEMA DEVICE-QUEUE-INTEGER DEVICE-QUEUE-ITEM", "裝置內容佇列資料不符合要求", "佇列內容、項目識別、傳送模式或狀態回報缺少有效資料。", "核對伺服器與韌體版本、裝置排程及傳送模式；重新取得最新佇列。")
_register("QUEUE-002", "佇列項目不屬於這個裝置或已失效", "系統無法驗證項目、發布版本與目前裝置的對應關係。", "讓裝置重新取得最新內容，不要重送其他裝置或舊版本的回報。")
_register("QUEUE-003 DEVICE-QUEUE-STALE", "裝置使用的內容佇列已過期", "伺服器已有更新的佇列版本，舊版本回報不能套用。", "重新取得最新清單，不要繼續顯示或完成舊項目。")
_register("QUEUE-004", "佇列項目的狀態已改變", "目前項目不允許這個狀態變更，可能已完成或已被替換。", "重新取得裝置與佇列狀態，避免重放舊操作。")
_register("DEVICE-QUEUE-ACK-KEY DEVICE-QUEUE-ACK-RETRY", "裝置的顯示確認尚未送達", "裝置未能保存重送識別資訊，或等待伺服器確認的連線失敗。", "檢查裝置儲存與網路，保留待確認事件，讓裝置安全重送。")
_register("PAIR-001 PAIR-003", "目前無法建立或繼續配對", "配對請求可能不存在、狀態已改變，或裝置不允許此配對方式。", "到裝置管理確認配對狀態，再由管理員開啟新的配對流程。")
_register("PAIR-002", "配對請求過於頻繁", "短時間內建立太多配對請求，系統暫時限制新請求。", "等待限制解除並先處理既有配對，不要連續按下配對。")
_register("PAIR-004 PAIR-005", "配對資料格式不合法", "配對碼、裝置能力、時區或排程欄位未通過檢查。", "確認輸入內容與韌體版本，依具體欄位說明修正後再配對。")
_register("PAIR-006", "配對碼不正確或嘗試次數已用完", "系統未能用目前配對碼確認裝置。", "核對裝置顯示的最新配對碼；次數用完時重新啟動配對。")
_register("PAIR-007", "配對請求或憑證已過期", "這個配對流程已超過有效時間或已終止。", "由管理員重新允許配對，再使用新配對碼。")
_register("PAIR-008 PAIR-009", "配對憑證或確認狀態無法驗證", "憑證不一致、暫存資料無效，或已完成的配對確認被重送。", "重新讀取配對狀態；若憑證失效，由管理員重新配對，不要反覆重送舊憑證。")
_register("PAIR-010", "正式配對必須使用加密連線", "目前連線沒有符合正式環境要求的 HTTPS 保護。", "使用正確的 HTTPS 伺服器網址與憑證後再配對。")
_register("PHOTOPAINTER-001 PHOTOPAINTER-002 PHOTOPAINTER-003 PHOTOPAINTER-004 PHOTOPAINTER-005 PHOTOPAINTER-006", "電子紙畫面資料與裝置協定不相容", "像素、色碼、尺寸、傳送模式或發布授權驗證失敗。", "核對面板規格及傳送模式，重新產生相容畫面；不要直接把錯誤畫面送到實體面板。")
_register("BACKUP-001 BACKUP-002 BACKUP-003", "備份無法建立或驗證", "備份格式、內容大小、校驗值或資料庫完整性未通過檢查。", "檢查磁碟與權限，保留現有資料；不要用未驗證備份覆蓋資料庫。")
_register("RESTORE-001", "還原前必須停止背景服務", "仍有 Web、Worker 或 Scheduler 使用資料庫，不能安全還原。", "請管理員停止三個服務，保留安全副本後再執行離線還原。")
_register("RESTORE-002 RESTORE-003 RESTORE-004 RESTORE-005 RESTORE-006", "備份還原未通過安全檢查", "備份結構、資料庫版本、完整性或還原前的安全副本不符合要求。", "保留原資料及備份，查看還原紀錄並改用相容的已驗證備份。")
_register("RECOVERY-001 RECOVERY-002 RECOVERY-003 RECOVERY-004 RECOVERY-005 RECOVERY-006", "敏感設定恢復資料無法驗證", "恢復密語、金鑰檔案或恢復包格式／完整性不正確。", "確認原恢復密語與安全備份，勿覆蓋現有金鑰；必要時由管理員處理。")
_register("RETENTION-001 RETENTION-002 RETENTION-003 CACHE-001 CLEANUP-001", "清理條件尚未通過安全確認", "清理參數不合法、尚未確認，或資料已在預估後改變。", "重新執行清理預估，核對將移除的範圍後再明確確認。")
_register("ROLLBACK-001 ROLLBACK-002 ROLLBACK-005 ROLLOUT-001 ROLLOUT-005 RELEASE-STUCK", "發布或回復流程需要處理", "裝置顯示失敗、缺少可回復版本，或發布階段與傳送模式不相容。", "查看發布活動與裝置結果，保留目前可用畫面，再選擇可驗證的發布或回復版本。")
_register("SET-001 SET-002 SET-003 SET-004 SET-005 SET-006 SET-007 PRESET-001 PRESET-002 PRESET-003 PRESET-004", "設定內容或確認條件不符合要求", "欄位值、預設樣式、快照或高風險變更的確認資料不合法。", "依具體欄位說明修正；高風險變更需重新預覽並確認，不要直接重送過期表單。")
_register("FEEDBACK-001 FEEDBACK-002 SHADOW-001", "回饋或比較設定無法套用", "照片識別、回饋類型、抽樣比例或儲存結果不符合要求。", "重新選擇照片與回饋／比較方式，再送出；若已寫入，先確認結果再重試。")
_register("SCHEDULE-002 SCHEDULE-003 SCHEDULE-STUCK", "排程設定或執行異常", "日期、時刻、裝置或工作狀態不符合目前排程要求。", "到低資源排程核對時區、裝置及最近執行結果，修正後再執行。")
_register("NOTIFY-001 NOTIFY-WEBHOOK", "通知設定或傳送失敗", "通知憑證、端點設定或遠端服務回覆有問題。", "檢查通知設定與傳送紀錄；確認遠端服務可用後再試。")
_register("WEBHOOK-SSRF-001 WEBHOOK-SSRF-002 WEBHOOK-SSRF-003 WEBHOOK-SSRF-004", "通知網址未通過網路安全檢查", "網址、DNS 或實際連線位址不合法，或指向不允許的內部網路。", "確認使用受信任的公開 HTTPS 通知網址，不要停用位址驗證。")
_register("PROVIDER-001 PROVIDER-002 PROVIDER-003 PROVIDER-004 PROVIDER-005 PROVIDER-006 PROVIDER-007 PROVIDER-008 PROVIDER-009 PROVIDER-010 PROVIDER-011 PROVIDER-012 PROVIDER-013 PROVIDER-014 PROVIDER-015 PROVIDER-016 PROVIDER-017 PROVIDER-018 PROVIDER-019 PROVIDER-020 PROVIDER-021", "模型服務的連線或路由設定不合法", "服務類型、模型名稱、網址或路由選項未通過安全及相容性檢查。", "在「模型服務」核對具體欄位；OpenRouter 需完整模型名稱，私有 HTTP 需明確授權。")
_register("IDEMPOTENCY_CONFLICT IDEMPOTENCY_LEDGER_INVALID IDEMPOTENCY_RESERVATION_LOST IDEMPOTENCY_IN_PROGRESS", "相同操作正在處理或內容已改變", "系統偵測到重複提交、不同內容共用識別資訊，或原提交的保留狀態已失效。", "先查看是否已建立工作；重新整理後再操作，不要快速重複點擊。")
_register("REVIEW-409", "照片已被另一個操作更新", "目前頁面上的版本已過期，系統未覆寫較新的結果。", "重新整理照片資料，再送出這次修改。")
_register("REVIEW-500", "無法讀取更新後的照片資料", "照片的顯示資料尚未完整建立，不能確認本次更新結果。", "先重新整理檢查是否已更新；持續失敗時查看資料庫與工作紀錄。")
_register("JSON-001 OPS-001", "提交內容格式不正確", "請求缺少有效資料，或欄位型別不符合要求。", "重新整理頁面並依欄位提示填寫，再送出。")
_register("HTTP-400 HTTP-405 HTTP-422", "這個請求無法處理", "網址、操作方式或提交資料不符合此功能要求。", "返回原頁面重新整理，核對欄位後再操作。")
_register("HTTP-401 HTTP-403", "目前無權存取這項內容", "登入狀態、驗證資料或帳號權限不符合要求。", "重新登入；需要管理權限時請管理員操作。")
_register("HTTP-404 HTTP-410", "找不到這項內容", "網址可能已失效，或資料已被移除。", "返回對應清單重新搜尋，不要重送舊網址。")
_register("HTTP-409", "資料狀態已改變", "這次操作與目前資料狀態衝突，尚未套用。", "重新整理頁面，確認最新狀態後再試。")
_register("HTTP-413", "上傳內容超過大小限制", "檔案或請求資料太大，伺服器無法接收。", "縮小檔案或分批上傳，並確認頁面的大小限制。")
_register("HTTP-429", "操作過於頻繁", "系統暫時限制大量重複請求。", "稍候再操作，避免快速重複點擊或同時開啟過多預覽。")
_register("HTTP-500 HTTP-502 HTTP-503 HTTP-504", "服務暫時無法完成操作", "伺服器遇到內部錯誤、忙碌或上游連線問題。", "先確認工作是否已建立；稍後重新整理，持續失敗時查看系統診斷。")
_register("NETWORK-ERROR", "無法連上伺服器", "網路連線中斷、伺服器重新啟動，或瀏覽器無法取得回應。", "確認網路及服務狀態，再重新整理；已送出的工作請先查看狀態，避免重複提交。")
_register("RESPONSE-INVALID", "伺服器回應無法辨識", "收到的內容不是預期的資料，可能是登入頁、代理錯誤頁或不完整回應。", "重新登入並重新整理；若仍發生，請管理員檢查服務與反向代理。")


_register("BATCH-001 BATCH-API-001 BATCH-API-002 BATCH-API-003 BATCH-API-004 BATCH-API-005 BATCH-API-006 BATCH-API-007 BATCH-API-008 BATCH-API-009 BATCH-SCOPE-001", "批次分析的操作或範圍不合法", "目前批次狀態、選片範圍或輸入欄位不允許這個操作。", "重新整理批次頁，依目前狀態與欄位提示操作。")
_register("BATCH-PROVIDER-001 BATCH-PROVIDER-002 BATCH-PROVIDER-003 BATCH-PROVIDER-004 BATCH-MODEL-001 BATCH-OPENROUTER-001", "沒有可用的批次分析模型服務", "服務可能不支援遠端批次、模型未指定，或保存的路由與目前設定不同。", "選擇支援遠端批次的服務及模型；OpenRouter 請使用一般即時分析工作。")
_register("BATCH-CANDIDATE-001", "沒有符合這次批次條件的照片", "候選照片可能已被其他批次預約、已分析或已排除。", "核對選片範圍及既有批次，避免重複提交相同照片。")
_register("BATCH-COST-UNKNOWN", "批次分析缺少完整價格資料", "系統無法估算整批成本，因此尚未提交批次。", "確認所選批次模型的實際輸入、快取及輸出價格，補齊後重新估算。")
_register("BATCH-INPUT-001 BATCH-INPUT-002 BATCH-INPUT-TOO-LARGE BATCH-FILE-001", "批次輸入檔無法建立或讀取", "照片或批次輸入檔可能缺失、過大，或檔案準備未完成。", "確認照片、磁碟空間及工作目錄；修正後重新建立批次，勿直接重送未知的遠端工作。")
_register("BATCH-FILE-002 BATCH-FILE-003 BATCH-FILE-004 BATCH-FILE-005 BATCH-OUTPUT-LINE-001", "批次檔案回應不完整或格式錯誤", "遠端檔案識別資訊、下載內容或結果資料列無法驗證。", "核對遠端檔案與批次識別資訊，再重新取得結果。")
_register("BATCH-REMOTE-001 BATCH-REMOTE-002 BATCH-REMOTE-003 BATCH-SUBMIT-001 BATCH-POLL-001 BATCH-JOB-START-001", "遠端批次無法提交或取得狀態", "遠端回應缺少必要識別資訊，或提交／狀態查詢未完成。", "先查看遠端是否已有相同批次，確認後再恢復，不要直接重複提交。")
_register("BATCH-UPLOAD-UNKNOWN BATCH-SUBMISSION-UNKNOWN BATCH-CANCEL-UNKNOWN BATCH-UNKNOWN-HOLD BATCH-SIDE-EFFECT-5XX BATCH-CLEANUP-UPLOAD-UNKNOWN BATCH-CLEANUP-SUBMISSION-UNKNOWN", "遠端批次操作的結果尚未確認", "上傳、提交或取消可能已在遠端生效，但本機沒有收到可確認的回覆。", "先在模型服務端核對批次及檔案識別資訊，再使用恢復或對帳流程；不要直接重新上傳或提交。")
_register("BATCH-ALREADY-CLAIMED BATCH-CLAIM-STALE BATCH-RESERVATION-CONFLICT", "另一個程序正在處理相同批次", "本次操作沒有取得有效處理權，或原處理權已過期。", "重新整理批次狀態並等待目前程序完成，避免同時重複操作。")
_register("BATCH-IMPORT-PLAN-001 BATCH-POLL-PLAN-001 BATCH-RECOVERY-PLAN-001 BATCH-UPLOAD-RECOVERY-PLAN-001", "批次保存的分析計畫不完整", "無法還原當初的模型、路由或分析規格，因此不能安全匯入或重送。", "保留遠端批次與本機紀錄，先核對原分析計畫，再由管理員恢復。")
_register("BATCH-REMOTE-IDENTITY-001 BATCH-REMOTE-IDENTITY-002 BATCH-REMOTE-IDENTITY-003 BATCH-PROVIDER-IDENTITY-001 BATCH-CLEANUP-PROVIDER-001 BATCH-CLEANUP-PROVIDER-LEGACY BATCH-CLEANUP-PROVIDER-UNKNOWN BATCH-CLEANUP-PROVIDER-MISMATCH", "批次的模型服務或遠端身分不一致", "目前帳號、專案、服務或遠端檔案與建立批次時保存的身分不符。", "核對原服務、帳號與批次識別資訊；未確認歸屬前不要刪除遠端檔案或重新綁定。")
_register("BATCH-RECOVERY-001 BATCH-RECOVERY-002 BATCH-RECOVERY-003 BATCH-RECOVERY-004 BATCH-RECOVERY-005 BATCH-RECOVERY-006 BATCH-RECOVERY-008 BATCH-RECOVERY-JOB-001 BATCH-RECOVERY-UPLOAD-001 BATCH-RECOVERY-PREPARING BATCH-RECOVERY-PREPARING-CLEANUP", "批次恢復條件尚未滿足", "原工作可能未準備完成，或遠端狀態／識別資訊與本機不一致。", "先核對批次目前狀態、原工作及遠端回應，再執行對帳或恢復。")
_register("BATCH-RECOVERY-OWNERSHIP-001 BATCH-RECOVERY-OWNERSHIP-002 BATCH-RECOVERY-OWNERSHIP-003 BATCH-RECOVERY-OWNERSHIP-004 BATCH-RECOVERY-OWNERSHIP-005 BATCH-RECOVERY-OWNERSHIP-006 BATCH-RECOVERY-OWNERSHIP-007 BATCH-RECOVERY-OWNERSHIP-008", "無法確認遠端批次屬於這個工作", "批次識別資訊、輸入檔、帳號或專案歸屬驗證未通過。", "核對原批次與輸入檔資訊；不要略過歸屬檢查或綁定其他工作。")
_register("BATCH-UPLOAD-RECOVERY-001 BATCH-UPLOAD-RECOVERY-002 BATCH-UPLOAD-RECOVERY-003 BATCH-UPLOAD-RECOVERY-004 BATCH-UPLOAD-RECOVERY-005 BATCH-UPLOAD-RECOVERY-006 BATCH-UPLOAD-RECOVERY-007 BATCH-UPLOAD-RECOVERY-008 BATCH-UPLOAD-RECOVERY-009 BATCH-UPLOAD-RECOVERY-010 BATCH-UPLOAD-RECOVERY-011", "無法確認先前上傳的批次檔案", "遠端檔案的名稱、大小、用途、歸屬或目前上傳狀態不符合原批次。", "在原模型服務帳號核對檔案資訊，確認後再恢復上傳紀錄，避免重複上傳。")
_register("BATCH-ABANDON-002 BATCH-ABANDON-CONFIRM-001 BATCH-ABANDON-REMOTE-001", "尚不能放棄這筆未知狀態的批次", "目前階段不允許放棄，或尚未明確確認遠端工作不存在。", "先核對遠端批次是否存在與是否已計費，再使用批次頁的確認流程。")
_register("BATCH-ABANDON-FILE-001 BATCH-ABANDON-FILE-002 BATCH-ABANDON-FILE-003 BATCH-ABANDON-FILE-004 BATCH-ABANDON-FILE-005 BATCH-ABANDON-FILE-006 BATCH-ABANDON-FILE-007 BATCH-ABANDON-FILE-008 BATCH-ABANDON-FILE-009", "批次檔案尚未通過放棄／清理驗證", "檔案識別資訊、名稱、大小或歸屬不符，或模型服務不支援驗證。", "先核對原帳號中的遠端檔案；不要刪除尚未確認歸屬的檔案。")
_register("BATCH-CLEANUP-001 BATCH-CLEANUP-TERMINAL-001 BATCH-CLEANUP-OPERATOR-REQUIRED", "批次檔案清理需要管理員確認", "遠端工作可能尚未結束，或清理安全條件不足。", "確認遠端終止狀態、檔案歸屬及結果已保存後，再進行清理。")
_register("BATCH-RETRY-001", "目前批次不能直接重試", "批次狀態或結果尚未滿足建立重試工作的條件。", "先完成原批次對帳，確認失敗項目後再建立重試批次。")
_register("PREFLIGHT-CONFIG-001 PREFLIGHT-DB-001 PREFLIGHT-DB-002 PREFLIGHT-HTTP-001 PREFLIGHT-HTTP-002 PREFLIGHT-HTTPS-001 PREFLIGHT-HTTPS-002 PREFLIGHT-LAN-001 PREFLIGHT-LAN-002 PREFLIGHT-LAN-003 PREFLIGHT-LAN-BUILD-001 PREFLIGHT-LAN-BUILD-002 PREFLIGHT-LAN-ENV-001 PREFLIGHT-LAN-MOUNT-001 PREFLIGHT-LAN-PATH-001 PREFLIGHT-LAN-PATH-002 PREFLIGHT-LAN-PATH-003 PREFLIGHT-PROXY-001 PREFLIGHT-URL-001 PREFLIGHT-PATH-001 PREFLIGHT-MOUNT-001 PREFLIGHT-BUILD-001", "部署設定未通過啟動前檢查", "資料路徑、掛載、資料庫、版本或連線安全設定與部署要求不一致。", "請管理員依檢查回報修正環境設定與掛載，重新執行啟動前檢查，不要略過安全限制。")
_register("missing_result", "遠端批次缺少這張照片的結果", "遠端已回傳結果檔，但其中沒有找到此照片的對應資料。", "先完成批次對帳，再只重試確認沒有結果的照片。")
_register("invalid_jsonl unexpected_custom_id", "批次結果檔無法正確對應照片", "結果資料列格式錯誤，或包含不屬於這次批次的照片識別資訊。", "確認下載的是本次批次的結果檔，重新對帳；不要將不明結果直接匯入。")
_register("cancelled abandoned", "這個項目已取消或放棄", "本機不會再繼續這個項目的正常處理流程。", "若曾送到遠端，請核對遠端最終狀態；需要處理時再建立新工作。")
_register("connect_timeout connect_failed read_timeout response_too_large redirect_rejected upload_failed", "裝置網路傳送未完成", "連線逾時、回應內容或實際傳送未通過檢查。", "檢查裝置與網路；若可能已收到畫面，先確認裝置狀態再決定重送。")
_register("release_not_authorized payload_invalid", "裝置無法取得授權的有效畫面", "畫面檔未通過發布授權或內容完整性檢查。", "重新核對裝置對應的發布版本，必要時重新發布；不要跳過授權或校驗。")


_register("BATCH-SUBMISSION-REJECTED BATCH-UPLOAD-REJECTED", "模型服務拒絕這次批次提交", "遠端明確拒絕檔案上傳或工作提交，未取得可用的批次結果。", "查看服務回覆，修正權限、模型或輸入檔後，再建立重試工作。")
_register("PAIR-CAPABILITY-CONFLICT", "裝置回報的能力與面板設定衝突", "裝置韌體、面板規格或支援的功能互相不一致，無法安全配對。", "核對實際面板與韌體設定，修正後重新提出配對，不要強行套用不相容規格。")
_register("NOT_PREPARED SCHEDULE-PREPARE-OVERDUE SCHEDULE-RELEASE-OVERDUE", "排程需要的畫面尚未準備完成", "已到準備或發布時刻，但前置工作尚未完成，可能影響準時換圖。", "查看排程及對應工作進度，確認分析、渲染與背景服務是否正常。")
_register("APPLICATION-ERROR", "這個操作未能完成", "提交資料或目前系統狀態不符合操作要求。", "依本次回報的具體原因修正；若狀態不明，先重新整理確認是否已套用。")
_register("current_password_invalid", "目前密碼不正確", "無法驗證原密碼，因此尚未變更密碼。", "重新輸入正確的目前密碼；忘記密碼時請管理員協助。")
_register("password_contains_nul password_invalid_type password_too_long password_too_short", "新密碼不符合要求", "密碼型別、字元或長度不符合帳號安全規則。", "依本次欄位提示重新設定足夠長且不含控制字元的密碼。")
_register("username_blank username_control_character username_invalid_characters username_invalid_type username_too_long username_too_short", "帳號名稱不符合要求", "帳號名稱是空白、含不允許的字元，或長度不符合規則。", "依表單與本次欄位提示修正帳號名稱後再送出。")
_register("username_taken", "這個帳號名稱已被使用", "已有相同名稱的帳號，不能重複建立。", "使用其他帳號名稱，或請管理員確認既有帳號。")
_register("last_administrator_required", "必須保留至少一位啟用中的管理員", "這次變更會讓系統沒有可用管理員，因此未套用。", "先建立或啟用另一位管理員，再調整此帳號。")
_register("enabled_invalid_type invalid_auth_input role_invalid user_update_invalid", "帳號設定資料不合法", "帳號角色、啟用狀態或提交欄位不符合要求。", "重新整理帳號管理頁，依欄位提示修正後再送出。")
_register("setup_already_completed", "系統已完成首次設定", "已有管理帳號，不能再透過首次設定流程建立管理員。", "請登入既有管理帳號，再從帳號管理新增使用者。")
_register("user_not_found", "找不到這個使用者帳號", "帳號可能已刪除，或頁面上的資料已過期。", "返回帳號管理重新選擇現有帳號。")
_register("webhook_connect_timeout webhook_read_timeout webhook_timeout", "通知服務沒有及時回覆", "連線或等待通知回應逾時，尚未確認通知是否送達。", "查看通知傳送紀錄與遠端服務，避免在系統自動重試時重複傳送。")


_register("AI-PROVIDER-COOLDOWN-STUCK", "工作等待模型服務冷卻已超過十五分鐘", "模型服務持續失敗或受限制，已影響佇列中的照片處理。", "查看模型服務與呼叫紀錄，核對額度及端點；增加 Worker 不會解除服務端限制。")
_register("BATCH-CLEANUP-RECONCILE", "無法確認批次檔案的遠端清理狀態", "清理時未取得可確認的遠端狀態，因此保留本機紀錄。", "核對原帳戶的遠端檔案，再執行對帳；不要自行把清理標成完成。")
_register("BATCH-HTTP-ERROR", "批次中的這張照片被模型服務拒絕", "遠端結果包含失敗回應，未取得可匯入的照片分析。", "查看此項目的服務回覆，修正原因後只重試失敗項目。")
_register("BATCH-PHOTO-MISSING", "批次結果對應的照片已不存在", "遠端結果無法對應目前照片庫中的原照片，未匯入此結果。", "核對照片是否移動或刪除，恢復來源後重新掃描。")
_register("BATCH-RESPONSE-BODY", "批次回覆沒有有效的分析內容", "服務雖回報請求成功，卻沒有回傳可解析的模型文字結果。", "核對遠端使用量與結果檔，確認後只重試缺少結果的照片。")
_register("BATCH-STALE BATCH-STALE-ANALYSIS-FINGERPRINT", "這筆批次結果已不適用目前照片或設定", "送出後照片內容或分析設定已變更，系統未用舊結果覆蓋新資料。", "核對照片及分析設定；需要新結果時重新建立分析工作。")
_register("DEVICE-DOWNLOAD-OVERDUE", "裝置尚未下載已指派的畫面", "伺服器已有新畫面，但尚未收到此裝置的下載紀錄。", "確認裝置電源、網路與預定喚醒時間，再查看裝置事件。")
_register("DEVICE-VERIFY-OVERDUE", "裝置下載後尚未回報檔案驗證結果", "已收到下載紀錄，但還不能確認畫面檔完整無誤。", "查看裝置網路、檔案校驗與韌體回報；不能把下載完成當成顯示成功。")
_register("DEVICE-ACK-OVERDUE", "尚未收到裝置完成換圖的確認", "畫面檔已下載並驗證，但裝置沒有回報顯示完成。", "查看實體面板與裝置回報；不要只憑下載成功判定換圖完成。")
_register("DEVICE-CONFIG-ACK-OVERDUE", "裝置尚未確認最新設定", "伺服器設定比裝置已確認的版本更新，無法確認裝置已套用。", "確認裝置是否已喚醒連線，並查看設定版本及裝置事件。")
_register("DEVICE-DISPLAY-FAILED", "裝置連續下載或顯示失敗", "裝置已多次無法取得或顯示指派畫面。", "查看裝置的最近錯誤，核對網路、畫面規格及實體面板，不要反覆發布相同失敗內容。")
_register("RENDER-PRESET-SIZE", "渲染預設資料超過儲存上限", "這次預覽可另行核對，但自訂預設資料太大，未能保存。", "減少自訂預設或設定內容後再儲存。")
_register("RENDER-PRESET-VALIDATION RENDER-PRESET-WRITE", "渲染預設未能儲存", "自訂設定未通過驗證或寫入失敗，不能確認已保存。", "重新整理預設清單確認結果，依欄位提示或資料庫狀態修正後再儲存。")
_register("RETENTION-CLEANUP-FAILED", "資料清理未完成", "清理程序回報異常，不能確認所有預定項目都已清理。", "查看清理紀錄與剩餘資料，重新預估後再確認，不要手動刪除整個資料目錄。")
_register("SCAN-CANCELLED SCAN-INCOMPLETE", "掃描已取消或未完整完成", "缺少完整掃描證據，不能據此將未出現的照片標示為遺失。", "確認照片來源正常後再執行完整掃描；現有照片與分析紀錄應保留。")
_register("SCHEDULE-PAST-SKIP", "已略過新排程中過去的時刻", "這是正常的排程保護，不會補播修改前已經過去的畫面。", "確認接下來的排程時刻即可，不需要重送過去的項目。")
_register("SCHEDULE-REPEATED-SKIP", "排程持續錯過執行時刻", "啟用中的排程未能在預定時間完成執行。", "查看排程最近失敗紀錄、工作佇列及背景服務，排除原因後再恢復。")


DEFAULT = {
    "title": "操作未完成，原因尚未分類",
    "detail": "系統或外部服務回報了尚未分類的錯誤，不能只由代碼判斷原因。",
    "action": "先查看目前結果，避免重複提交；保留技術資料與發生時間，交由管理員查核。",
}
CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+)(?![A-Za-z0-9_])")
CHINESE = re.compile(r"[\u3400-\u9fff]")
QUOTED_SECRET = re.compile(
    r'''(?i)(["'](?:api[_-]?key|x[_-]?api[_-]?key|token|password|passwd|secret|authorization|cookie|credential|access[_-]?token|refresh[_-]?token)["']\s*:\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
)


def _diagnostic_text(value: object) -> str:
    raw = QUOTED_SECRET.sub(lambda match: match.group(1) + '"[已遮蔽]"', str(value or ""))
    return bounded_text(raw, maximum_bytes=6000)[:1500]


def plain_message(message: object) -> str:
    """Translate embedded known codes without turning informational events into failures."""
    raw = _diagnostic_text(message)
    return CODE_PATTERN.sub(lambda match: CATALOG.get(match.group(), {}).get("title", match.group()), raw)


def explain_error(code: object = "", message: object = "", http_status: object = None) -> dict:
    """Return an honest explanation, preserving codes only in technical fields."""
    raw = _diagnostic_text(message)
    key = _diagnostic_text(code).strip()[:128]
    if not key:
        match = CODE_PATTERN.match(raw)
        if match and (match.group() in CATALOG or re.search(r"-\d{3}$", match.group())):
            key = match.group()
    if not key and re.search(r"Failed to fetch|NetworkError|Load failed", raw, re.IGNORECASE):
        key = "NETWORK-ERROR"
    if not key and http_status:
        key = f"HTTP-{http_status}"
    entry = dict(CATALOG.get(key, DEFAULT))
    lowered = raw.lower()
    if key == "VLM-008" and ("截圖" in raw or "禁止上傳" in raw or "never_upload" in raw):
        entry = {"title": "這張照片不允許傳送給模型", "detail": "照片被確認為截圖，或已設定禁止上傳；這是照片的隱私保護條件，不是模型故障。", "action": "到照片詳情核對本機預篩選與隱私設定；確認原因前不要重複提交。"}
    elif key == "VLM-008" and "AI 模式目前為關閉" in raw:
        entry = dict(CATALOG["ANALYSIS-DISABLED"])
    elif "no endpoints found that can handle" in lowered:
        entry = {"title": "模型路由目前沒有相容端點", "detail": "模型服務找不到能同時支援本次圖片與輸出格式的端點。", "action": "稍後再試，或改用固定且支援圖片與結構化輸出的模型；增加 Worker 不會解除這個限制。"}
    elif "display_suitability_grade" in raw:
        entry = {"title": "模型回覆的電子紙適合度格式不正確", "detail": "這是模型欄位格式問題，不是照片損壞；合法值為 S、A、B、C、D、E、unknown 或 null（無法判斷）。", "action": "舊版曾誤拒絕 null，目前已修正；若仍失敗，查看技術資料中的實際回覆再重試。"}
    elif (str(http_status) == "429" or re.search(r"\b429\b", raw)) and key.startswith(("VLM", "AI-PROVIDER")):
        entry = dict(CATALOG["VLM-002"])
    elif key.startswith(("VLM", "AI-PROVIDER")) and str(http_status) in {"401", "403"}:
        entry = dict(CATALOG["AUTH_REQUIRED"])
    elif key.startswith(("VLM", "AI-PROVIDER")) and str(http_status) == "402":
        entry = {"title": "模型服務的帳號額度不足", "detail": "服務端拒絕這次請求，回報需要額度或付款；這不是背景工作數量不足。", "action": "核對服務端帳戶額度及實際選用模型；免費路由也需遵守服務端限制。"}
    elif key.startswith(("VLM", "AI-PROVIDER")) and str(http_status) in {"500", "502", "503", "504"}:
        entry = {"title": "模型服務端暫時發生故障", "detail": "上游服務回報內部錯誤或逾時，暫時無法取得有效分析結果。", "action": "先查看呼叫紀錄與遠端使用量；等待冷卻或更換可用模型，避免連續重送。"}
    # Keep useful specific validation detail, but never surface codes or raw
    # HTML/JSON/tracebacks as the primary explanation. Raw text stays collapsed.
    specific = raw
    if key:
        specific = specific.replace(key, "")
    specific = CODE_PATTERN.sub(lambda m: CATALOG.get(m.group(), {}).get("title", "SHA-256" if m.group() == "SHA-256" else "相關檢查"), specific).strip(" ：:｜|—-\n")
    if not key and CHINESE.search(specific):
        entry["title"] = "操作未完成"
    if (CHINESE.search(specific) and not any(token in specific for token in ("Traceback", "<", ">", "{", "\\n")) and specific not in (entry["title"], entry["detail"])):
        entry["detail"] += " 本次回報：" + specific[:500]
    return {**entry, "message": "。".join([entry["title"].rstrip("。"), entry["detail"].rstrip("。"), entry["action"].rstrip("。")]) + "。", "code": key, "technical_message": raw, "known": key in CATALOG}


def error_text(code: object = "", message: object = "", http_status: object = None) -> str:
    return str(explain_error(code, message, http_status)["message"])
