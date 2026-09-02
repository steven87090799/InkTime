from __future__ import annotations

from typing import Any, Iterable


SCOPE_LABELS = {
    "dynamic": "儲存後立即生效",
    "next_job": "下一次工作開始時生效",
    "next_render": "下一次渲染或建立 Release 時生效",
    "future_device_only": "只影響之後新增的裝置",
    "restart": "重新啟動服務後生效",
    "not_wired": "目前尚未接上執行流程，不可修改",
}

RISK_LABELS = {"low": "低風險", "medium": "中風險", "high": "高風險"}

CHOICE_EXPLANATIONS: dict[str, dict[Any, tuple[str, str]]] = {
    "observability.detail_level": {
        "normal": ("一般", "保留日常排錯需要的摘要，紀錄量較少。"),
        "detailed": ("詳細", "記錄更多執行細節，較容易排錯，但會增加日誌與資料量。"),
    },
    "analysis.strategy": {
        "local": ("僅本機分析", "只做本機品質與選片，不呼叫 Vision Provider，也不產生模型費用。"),
        "single": ("單次完整分析", "每張入選照片用一次完整 Vision 請求產生評分與文案。"),
    },
    "analysis.copy_default_style": {
        "natural": ("自然", "使用平實、接近日常說話的照片短句。"),
        "warm": ("溫暖", "語氣較柔和、有溫度，但仍不可猜測照片未提供的事實。"),
        "literary": ("文學", "語句較有意象，可能比自然風格更抽象。"),
        "humorous": ("幽默", "使用輕鬆語氣；不適合嚴肅或不確定的照片情境。"),
        "minimal": ("極簡", "盡量縮短文字，適合電子紙空間有限的版型。"),
    },
    "analysis.execution_mode": {
        "disabled": ("完全停用", "不建立任何照片分析流程；既有結果仍保留。"),
        "local_only": ("僅使用本機選片", "只執行本機特徵與品質判斷，不會使用 Provider。"),
        "local_with_manual_ai": ("本機選片＋手動 AI", "平時只做本機分析；只有你按下送入 AI 或建立 AI 工作時才呼叫模型。"),
        "automatic_ai": ("自動 AI 分析", "排程或新工作可自動使用已啟用的 Vision Provider，可能產生 Token 與費用。"),
    },
    "analysis.ai_mode": {
        "off": ("關閉 AI", "即使 Provider 已設定也不會把照片送入模型。"),
        "top_candidates": ("只分析前 N 張候選", "先用本機分數排序，只把最高分的一小部分送入 AI，較省成本。"),
        "eligible": ("分析所有合格照片", "所有通過本機門檻的照片都可進入 AI，處理量通常較大。"),
        "full_library": ("分析完整照片庫", "忽略一般候選縮減策略；必須在建立工作時再次確認，成本最高。"),
        "on_demand": ("按需分析", "只在手動操作時送入 AI，不由一般排程自動執行。"),
    },
    "analysis.prefilter_profile": {
        "conservative": ("保守", "寧可保留較多照片，降低誤排除，但 AI 候選與成本可能增加。"),
        "balanced": ("平衡", "在排除低品質照片與保留候選之間採用預設折衷。"),
        "aggressive": ("積極", "更嚴格排除低品質照片，可省成本，但較可能漏掉仍有意義的照片。"),
    },
    "model.reasoning_effort": {
        "none": ("不使用額外推理", "要求模型直接處理；速度和相容性通常最好。"),
        "low": ("低", "使用少量推理預算。"),
        "medium": ("中", "使用中等推理預算，模型必須支援。"),
        "high": ("高", "使用較多推理預算，延遲與 Token 可能增加。"),
        "xhigh": ("極高", "只適合明確支援此等級的模型，否則可能請求失敗。"),
        "max": ("最大", "使用模型允許的最高推理預算，最可能增加延遲與成本。"),
    },
    "render.history_layout_source": {
        "history_today": ("歷史上的今天", "優先使用日期相近的回憶照片。"),
        "top_ranked": ("最高排序", "忽略日期接近度，優先使用綜合排序較高的照片。"),
    },
    "render.history_fallback_mode": {
        "nearby_then_ranked": ("鄰近日再退回排序", "先找日期相近照片，沒有合適候選時使用最高排序照片。"),
        "nearby_only": ("只用鄰近日", "找不到日期相近照片時不以一般高分照片替代。"),
        "ranked": ("直接使用排序", "不要求日期接近，直接選綜合分最高的照片。"),
        "none": ("不使用替代", "找不到主要候選時停止，不自動挑另一張。"),
    },
    "render.frame_orientation": {
        "portrait": ("直向", "相框長邊垂直；版面會以直向尺寸重新配置。"),
        "landscape": ("橫向", "相框長邊水平；版面會以橫向尺寸重新配置。"),
    },
    "render.fit_mode": {
        "stretch_fill": ("填滿照片區（不裁切）", "保留完整畫面並填滿區域，但可能有輕微比例變形。"),
        "contain": ("完整顯示", "保持照片比例且不裁切，邊緣可能留白。"),
        "cover": ("填滿並裁切", "保持比例並填滿區域，超出邊界的內容會被裁掉。"),
    },
    "render.color_distance": {
        "oklab": ("OKLab 感知色差", "依人眼感知接近度挑電子紙顏色，通常較自然。"),
        "rgb": ("RGB 距離", "直接比較紅綠藍數值，較單純但不一定符合人眼感受。"),
    },
    "observability.log_level": {
        "DEBUG": ("除錯", "記錄最多細節；只建議短期排錯使用。"),
        "INFO": ("一般資訊", "記錄正常流程與重要狀態，適合日常使用。"),
        "WARNING": ("警告以上", "只記錄可能有問題或更嚴重事件。"),
        "ERROR": ("錯誤以上", "只保留失敗與嚴重故障，日常脈絡較少。"),
        "CRITICAL": ("只記錄嚴重故障", "紀錄最少，可能不足以追查一般錯誤。"),
    },
    "observability.log_format": {
        "human": ("人類可讀", "適合直接閱讀終端與日誌文字。"),
        "json": ("JSON 結構化", "適合日誌平台解析；直接閱讀較不直觀。"),
    },
}


PANEL_PROFILE_CHOICES = {
    "safe_4c": ("安全四色", "使用保守的四色輸出，適合不確定面板能力時測試。"),
    "gdep073e01_6c": ("GDEP073E01 六色", "輸出給對應的 Good Display 7.3 吋六色面板；裝置 Profile 必須一致。"),
    "gdey073d46_7c": ("GDEY073D46 七色", "輸出給對應的 Good Display 7.3 吋七色面板；裝置 Profile 必須一致。"),
}


ACTION_GROUPS: tuple[dict[str, Any], ...] = (
    {"page": "共用操作", "href": "/dashboard", "actions": (
        ("登入", "驗證本機 InkTime 帳號並進入管理介面。", "成功後建立受保護的登入工作階段。", "連續失敗會暫時限制來源 IP；共享電腦使用後請登出。"),
        ("確認", "執行對話框中顯示的動作。", "可能寫入資料或改變狀態；送出前先重讀確認文字。", "危險程度取決於對話框內容。"),
        ("取消／關閉", "離開對話框或放棄尚未送出的動作。", "不會套用這次未確認的變更。", "已經成功送出的後端操作不會因此自動撤銷。"),
        ("深色／淺色模式", "切換目前瀏覽器的顯示配色。", "只影響畫面，不改變照片、設定或裝置。", "偏好會儲存在這個瀏覽器。"),
        ("登出", "結束目前登入工作階段。", "需要重新登入才能繼續管理。", "共用電腦使用完畢後建議登出。"),
    )},
    {"page": "設定控制中心", "href": "/settings", "actions": (
        ("匯出安全設定", "下載可搬移的一般設定檔。", "不會改變目前設定，也不會包含 API Key 或 Token。", "搬到另一台主機前仍要檢查版本差異。"),
        ("匯入設定", "讀取先前匯出的設定並先顯示差異。", "確認後才會寫入多項設定。", "不會匯入秘密資料；高風險項目要另外確認。"),
        ("一鍵套用 Spectra 6 建議值", "套用微雪 7.3 吋 Spectra 6 的建議色盤與抖動組合。", "會同時修改多個渲染設定，下一個 Release 才會反映。", "先預覽差異並確認實際面板型號。"),
        ("清除搜尋與篩選", "清空設定頁的搜尋、分類、風險與生效方式。", "只改變畫面顯示，不會修改設定。", "找不到項目時可先使用。"),
        ("恢復此項／此分類預設", "把尚未儲存的欄位改回程式安全預設。", "按下後仍需按「儲存變更」才會生效。", "分類恢復會一次影響多個欄位。"),
        ("放棄變更", "撤銷本頁所有尚未儲存的修改。", "已儲存的設定不受影響。", "無法只保留其中一部分未儲存修改。"),
        ("預覽影響", "檢查差異、驗證、重啟、重分析、重渲染與成本影響。", "只計算不寫入。", "高風險設定應先預覽。"),
        ("儲存變更", "寫入所有未儲存設定並建立 Snapshot。", "新值依各設定的生效方式套用。", "高風險變更會要求再次確認。"),
        ("查看 Diff／Rollback", "查看歷史 Snapshot 差異，或把可還原項目恢復到當時值。", "Rollback 會建立另一筆設定變更。", "先預覽，不會還原秘密或已移除設定。"),
        ("儲存／清除 Webhook Token", "更新或移除通知服務使用的秘密 Token。", "會影響之後的 Webhook 認證。", "Token 不會回顯；清除後通知可能無法送達。"),
        ("傳送測試通知", "用目前 Webhook 設定送出一筆測試。", "會真的連線到通知端點。", "這不是正式事件，但外部服務會收到請求。"),
    )},
    {"page": "模型服務", "href": "/providers", "actions": (
        ("新增／編輯 Provider", "建立或修改模型服務、Base URL、模型和認證資料。", "儲存後會影響可用 AI 路由。", "完整模型 ID 與 Vision 能力必須正確。"),
        ("Level 1 連線測試", "只檢查認證、端點與模型清單，不傳照片。", "通常不產生 Vision 圖片費用。", "成功只代表基本連線，不代表影像分析可用。"),
        ("Level 2 Vision 測試", "傳送合成測試圖片驗證 Vision 請求。", "會真的呼叫模型，可能產生 Token 或費用。", "先通過 Level 1，再確認供應商計價。"),
        ("Level 3 真實照片測試", "用實際照片驗證完整分析輸出。", "會把所選照片傳到 Provider，並可能產生費用。", "確認照片隱私與模型可用後才執行。"),
        ("模型價格", "設定輸入、輸出或圖片的計價資料。", "影響成本報表估算，不會改變 Provider 實際帳單。", "未知價格應保持未定價，不要填猜測值。"),
        ("儲存 Provider／價格", "寫入目前 Provider 或計價表單。", "可能使 AI 路由變成可用或不可用。", "儲存後仍建議按層級測試。"),
    )},
    {"page": "照片庫與詳情", "href": "/photos", "actions": (
        ("篩選／前往", "依狀態、類型、分數或頁碼縮小照片清單。", "只改變目前顯示範圍。", "篩選不會重新分析照片。"),
        ("清理縮圖快取", "刪除可重新產生的縮圖快取。", "可釋放空間，原始照片與正式 Release 不受影響。", "之後開啟照片時可能需要重新產生縮圖。"),
        ("儲存人工修正", "保存拍攝時間、描述、狀態或其他人工覆寫。", "人工值會優先於自動判斷。", "只在有證據時修正，並保留原因。"),
        ("儲存上傳隱私", "決定這張照片是否允許傳到外部 Provider。", "禁止時 AI 工作必須跳過該照片。", "本機分析仍可執行。"),
        ("旋轉 0°／90°／180°／270°", "設定照片顯示方向。", "之後的預覽與渲染會採用選定角度。", "不會改寫原始照片檔。"),
        ("清除人工設定", "移除這張照片的人工覆寫。", "畫面會重新採用自動分析或原始資料。", "已清除的人工內容無法靠此按鈕復原。"),
    )},
    {"page": "排除照片", "href": "/photos/excluded", "actions": (
        ("恢復為合格", "解除排除狀態，讓照片重新成為可用候選。", "不會自動送入 AI；若本機證據仍不合格，後續流程可能再次排除。", "先查看排除原因是否已不適用。"),
        ("重新本地分析", "重新計算模糊、曝光、截圖與電子紙適合度等本機證據。", "不會呼叫外部模型，也不產生 AI 費用。", "適合規則更新或原先分析不完整時使用。"),
        ("送入 AI／批次送入 AI", "為允許上傳且不是已確認截圖的照片建立 AI 工作。", "會呼叫 Vision Provider，可能產生 Token 與費用。", "截圖、永久排除或禁止上傳的照片會被阻擋。"),
        ("加入最愛", "將照片標示為最愛。", "排序時可取得最愛加分，但不會自動解除所有品質限制。", "最愛不是 AI 分析按鈕。"),
        ("加入候選池", "讓照片進入後續選片候選。", "提高被顯示流程考慮的機會，不保證一定發布。", "本機或隱私硬性限制仍然有效。"),
        ("永久排除", "將照片標示為長期不再使用。", "一般掃描與選片不會自動恢復。", "影響較長期；不確定時先保留在待確認。"),
    )},
    {"page": "Review 工作台", "href": "/review/photos", "actions": (
        ("接受／排除／待確認", "記錄人工對照片資格的判斷。", "人工狀態會影響後續候選與 Review 清單。", "證據不足時用待確認，不必勉強二選一。"),
        ("加入／移出候選池", "調整照片是否參與後續選片。", "只影響候選資格，不等同發布。", "硬性排除規則仍可能阻擋照片。"),
        ("加入／移出最愛", "調整最愛標記。", "可能影響排序加分。", "不會直接改變 AI 分數。"),
        ("理解錯誤／文案不好／分數不合理", "回報模型結果的具體問題類型。", "留下可供規則改善與稽核的回饋。", "請選最符合實際問題的一項。"),
        ("保存短文案", "儲存人工撰寫的電子紙短句。", "之後顯示時可優先使用人工文案。", "不要加入無法從照片確認的個資或事件。"),
        ("載入更多", "取得下一批符合篩選條件的照片。", "只增加目前頁面內容。", "不會建立分析工作。"),
    )},
    {"page": "分析工作", "href": "/jobs", "actions": (
        ("建立工作／確認建立", "依照片範圍、策略與上限建立分析工作。", "可能排入本機與 AI 處理，AI 模式可能產生費用。", "先確認 AI 就緒狀態、範圍與預估量。"),
        ("啟動", "開始處理尚未啟動的工作。", "Worker 會開始取得項目。", "先確認 Provider 與預算限制。"),
        ("暫停", "要求工作在安全邊界停止取得新項目。", "已在執行的單項可能先完成。", "暫停不會刪除已完成結果。"),
        ("繼續", "讓已暫停工作繼續取得項目。", "後續分析會接著執行。", "先確認原本暫停原因已排除。"),
        ("取消", "停止尚未處理的工作項目。", "已完成成果保留；未處理項目不再自動執行。", "需要重做時通常要另建工作。"),
        ("重跑失敗項目", "只把失敗項目重新排入，而不是重跑全部。", "會再次執行對應步驟，AI 項目可能再次計費。", "必須先修正錯誤根因。"),
        ("匯出結果", "下載目前工作的結果資料。", "不會改變工作狀態。", "秘密內容仍依系統遮罩規則處理。"),
    )},
    {"page": "Batch 分析", "href": "/analysis/batches", "actions": (
        ("估算", "計算 Batch 可能包含的照片數、檔案量與限制。", "不會提交到 Provider。", "先估算再建立大量工作。"),
        ("提交", "上傳並提交 Provider Batch。", "會建立外部批次，可能產生費用。", "避免重複提交相同範圍。"),
        ("重試失敗／復原上傳／復原提交", "只重做卡住或失敗的 Batch 階段。", "可能重新上傳或重新呼叫 Provider。", "先依詳情頁顯示的階段選對復原動作。"),
        ("重試清理", "重新清理已完成或放棄批次的暫存檔。", "不會重新分析照片。", "確認 Batch 結果已安全保存。"),
        ("Abandon 並清理", "把無法繼續的 Batch 標為放棄並移除可清理暫存。", "該批次不再繼續處理。", "這是高影響操作；不確定時先保留。"),
    )},
    {"page": "AI 即時追蹤", "href": "/ai/traces", "actions": (
        ("套用篩選／清除", "依 Provider、模型、狀態、照片或 Trace ID 查詢。", "只改變顯示內容。", "排查模型問題時保留 Trace ID。"),
        ("暫停畫面", "停止瀏覽器自動取得新 Trace。", "後端 AI 工作仍會繼續。", "恢復畫面後再讀取新資料。"),
        ("Raw／Parsed／Final", "切換原始回應、解析結果與最終採用內容。", "只切換檢視，不修改資料。", "秘密資料會遮罩，Raw 也不代表未經安全處理。"),
    )},
    {"page": "評分控制中心", "href": "/scoring", "actions": (
        ("套用範本", "把一組預先設計的權重與規則填入編輯器。", "仍需儲存新版本才會正式生效。", "範本是起點，不是所有相簿的標準答案。"),
        ("使用規則測試", "用測試照片或資料檢查目前未儲存規則。", "不會修改正式版本。", "先測試邊界案例與排除規則。"),
        ("儲存新版本", "保存目前規則與權重為新的可追溯版本。", "之後新分析會使用新版本；舊結果不會自動重算。", "Vision v4 固定權重：回憶 50%、視覺 25%、本機品質 25%。"),
        ("還原版本", "建立一個採用舊內容的新版本。", "不會刪除中間版本。", "還原後仍需重新分析才會改變既有照片分數。"),
    )},
    {"page": "相框、模擬與發布", "href": "/rendering", "actions": (
        ("套用相框 Preset", "載入預設的版型、色盤與抖動組合。", "會改變目前編輯值。", "確認 Profile 與實際面板一致。"),
        ("儲存相框設定／裁切", "保存版型或單張照片的裁切位置。", "下一次預覽或 Release 會採用新值。", "原始照片不會被改寫。"),
        ("恢復自動裁切", "移除人工裁切，交回自動構圖。", "下一次渲染重新計算裁切。", "不會恢復已刪除的 Release。"),
        ("產生 Preview／A-B 預覽", "產生不同算法或方向的比較畫面。", "只建立預覽，不會正式發布。", "一般螢幕預覽仍需搭配實體電子紙驗收。"),
        ("發布目前／全部 Profile", "依目前設定建立正式 Release。", "裝置之後可下載新內容；全部 Profile 影響較廣。", "先檢查 Preview、裝置相容性與裁切。"),
        ("立即建立 Release", "立即執行一次正式內容建立。", "會寫入 Release 紀錄並可能進入裝置流程。", "與 Preview 不同，這是正式狀態變更。"),
        ("傳送測試", "把測試畫面送到虛擬或指定測試目標。", "可能建立測試傳送紀錄，不等同正式排程。", "先確認目標裝置或虛擬墨水屏。"),
        ("上傳字型", "新增可供相框文字使用的字型檔。", "之後渲染可選用該字型。", "只上傳有權使用且格式受支援的字型。"),
        ("回滾 Release", "把正式內容切回指定的既有 Release。", "裝置下一次更新會取得回滾內容。", "先核對 Release、Profile 與裝置範圍。"),
        ("隨機一天／同日重抽", "改變模擬器使用的日期或當日照片組合。", "只影響預覽候選。", "不會改變正式排程。"),
    )},
    {"page": "裝置管理", "href": "/devices", "actions": (
        ("新增裝置", "建立裝置紀錄並開始配對流程。", "會產生新的裝置身分。", "先確認面板型號與實際裝置。"),
        ("核准／拒絕配對", "接受或拒絕待配對裝置。", "核准後裝置可取得設定；拒絕則無法完成配對。", "核對畫面代碼與實體裝置，避免核准錯誤設備。"),
        ("撤回／重新配對／撤銷", "終止目前配對授權或建立新的配對流程。", "舊憑證可能失效，裝置需重新完成配對。", "實體裝置不在手邊時不要任意重設。"),
        ("重生 Legacy Token", "產生新的舊版裝置存取 Token。", "舊 Token 會失效。", "只給仍需要 Legacy API 的受控裝置。"),
        ("立即顯示最新 Release", "要求裝置下一次連線時取得最新相容內容。", "可能提前改變螢幕顯示。", "離線裝置要等下次連線。"),
        ("儲存裝置設定", "保存面板 Profile、時區、排程與允許的覆寫。", "裝置會在後續同步取得新設定版本。", "觀察設定版本 ACK，不能只看 Web 已儲存。"),
        ("我已安全儲存", "確認一次性顯示的秘密資料已另行保存。", "關閉後通常不再完整顯示。", "不要把 Token 放進網址、截圖或日誌。"),
    )},
    {"page": "維護、排程與備份", "href": "/maintenance", "actions": (
        ("建立增量掃描工作", "掃描照片來源的新增或變更檔案。", "會更新照片索引，不會自動代表已完成 AI。", "照片掛載應保持唯讀。"),
        ("掃描並送到虛擬墨水屏", "串接掃描、必要處理與測試顯示流程。", "可能建立多個背景工作。", "先確認沒有重複的大型工作。"),
        ("儲存此排程", "保存工作類型、cron、時區與啟用狀態。", "Scheduler 之後依新時間執行。", "停用排程不會取消已開始的工作。"),
        ("立即建立背景工作", "不等下一個排程時間，現在建立一次工作。", "會增加 Queue 負載。", "避免與其他大型工作重疊。"),
        ("預覽 Playlist", "查看換圖排程會選到哪些內容。", "不會建立正式 Release。", "預覽結果仍受當時候選與日期影響。"),
        ("預估容量／立即清理", "先估算快取清理量，或立即刪除可重建快取。", "清理可釋放空間，但之後需要重建快取。", "不會刪除原始照片；仍要核對頁面顯示範圍。"),
        ("立即備份", "建立資料庫與可安全保存資料的 ZIP 備份。", "會新增備份檔並占用空間。", "預設不含原始照片與秘密資料，還要另外備份照片來源。"),
    )},
    {"page": "監控、錯誤與診斷", "href": "/activity", "actions": (
        ("套用／清除篩選", "依嚴重度、元件、工作、照片或文字查事件。", "只影響目前畫面。", "排錯時記下時間、錯誤碼與 Trace ID。"),
        ("暫停畫面", "停止自動更新事件列表。", "後端工作與事件寫入仍然繼續。", "適合閱讀某一刻的時間線。"),
        ("標記已解決", "為錯誤聚合填入處理備註並標示已處理。", "只改變錯誤中心狀態，不會修復根因。", "確認問題真的排除後再使用。"),
        ("建立／下載診斷包", "收集健康、版本與安全處理後的診斷資訊。", "會建立可下載檔案。", "分享前仍要確認接收者與可能包含的路徑資訊。"),
    )},
    {"page": "決策與韌性", "href": "/decision-traces", "actions": (
        ("儲存 Shadow", "設定新決策流程的背景抽樣比例與上限。", "可能增加運算，但不直接改變正式發布。", "證據足夠後再決定是否正式切換。"),
        ("建立 Queue", "為裝置準備離線可播放的相容 Release 佇列。", "會占用裝置儲存與同步量。", "Profile 不相容的 Release 不應加入。"),
        ("Dry Run", "預估資料保留策略會清理哪些資料。", "不會刪除資料。", "正式儲存較短保留期前先執行。"),
        ("建立活動／Start", "建立並開始 Canary 分階段發布。", "先把 Release 推向小範圍裝置。", "開始前核對 Release 與目標群組。"),
        ("Approve", "核准 Canary 進入下一階段或更大範圍。", "會擴大受影響裝置數。", "先看健康指標與失敗事件。"),
        ("Pause／Resume", "暫停或繼續 Canary 活動。", "控制後續擴散，不會抹除已完成階段。", "恢復前確認暫停原因已排除。"),
        ("Rollback", "把活動切回先前安全 Release。", "會改變正式裝置內容。", "這是高影響操作，先核對範圍與目標版本。"),
        ("送出回饋", "記錄喜歡、不喜歡、暫時跳過、永不顯示或恢復。", "會影響後續照片決策。", "不確定時先用暫時跳過。"),
    )},
    {"page": "虛擬墨水屏", "href": "/virtual-display", "actions": (
        ("上一張／下一張", "切換已收到的虛擬顯示畫面。", "只改變目前檢視，不會建立新 Release。", "沒有其他畫面時按鈕可能不可用。"),
    )},
)


def _display_value(value: Any) -> str:
    if isinstance(value, dict) and "status" in value:
        return "已設定" if str(value.get("status")) == "configured" else "未設定"
    if isinstance(value, bool):
        return "已啟用" if value else "已停用"
    if value is None or value == "":
        return "未設定"
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value) if value else "空清單"
    if isinstance(value, dict):
        return "已設定結構化內容"
    return str(value)


def _setting_choice_rows(key: str, definition: dict[str, Any]) -> list[dict[str, str]]:
    if definition.get("type") == "boolean":
        return [
            {"value": "true", "label": "啟用", "effect": "開啟這項功能；實際影響請配合本設定的用途與風險閱讀。"},
            {"value": "false", "label": "停用", "effect": "關閉這項功能；既有資料通常保留，不代表會回復先前結果。"},
        ]
    choices = list(definition.get("choices") or [])
    labels = definition.get("choice_labels") or {}
    explanations = dict(CHOICE_EXPLANATIONS.get(key, {}))
    if key in {"device.default_panel_profile", "render.panel_profile"}:
        explanations.update(PANEL_PROFILE_CHOICES)
    result = []
    for choice in choices:
        label, effect = explanations.get(
            choice,
            (str(labels.get(choice, choice)), "這是系統可接受的值；選用後依本設定的用途與生效方式套用。"),
        )
        result.append({"value": str(choice), "label": str(label), "effect": str(effect)})
    return result


def setting_entries(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    for row in rows:
        definition = dict(row.get("definition") or {})
        if not definition:
            continue
        key = str(row["key"])
        control_center = bool(definition.get("control_center"))
        href = "/scoring" if control_center else f"/settings?search={key}#setting-{key.replace('.', '-')}"
        effects = [SCOPE_LABELS.get(str(definition.get("effective_scope")), "依頁面說明生效")]
        if definition.get("reanalysis_impact"):
            effects.append("可能需要重新分析，既有結果不會自動重算")
        if definition.get("rerender_impact"):
            effects.append("需要建立新的預覽或 Release 才會反映")
        if definition.get("cache_impact"):
            effects.append("會改變 AI Cache Fingerprint，後續請求可能無法沿用舊快取")
        if definition.get("device_override_allowed"):
            effects.append("單一裝置或 Preview 可能用自己的覆寫值取代系統值")
        entries.append(
            {
                "id": f"setting-{key.replace('.', '-')}",
                "kind": "setting",
                "category": str(row.get("category") or definition.get("category") or "其他設定"),
                "label": str(definition.get("label_zh_tw") or key),
                "key": key,
                "description": str(definition.get("description") or "尚無說明"),
                "current": _display_value(row.get("value")),
                "default": _display_value(definition.get("default")),
                "risk": str(definition.get("risk") or "medium"),
                "risk_label": RISK_LABELS.get(str(definition.get("risk")), "未分類"),
                "risk_description": str(definition.get("risk_description") or "請在安全範圍內調整。"),
                "effect": "；".join(effects) + "。",
                "scope_label": SCOPE_LABELS.get(str(definition.get("effective_scope")), "依頁面說明生效"),
                "options": _setting_choice_rows(key, definition),
                "href": href,
                "action_label": "前往評分控制中心" if control_center else "前往這項設定",
                "editable": bool(definition.get("runtime_wired", True)),
            }
        )
    return entries


def action_entries() -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    index = 0
    for group in ACTION_GROUPS:
        for label, purpose, effect, caution in group["actions"]:
            index += 1
            result.append(
                {
                    "id": f"action-{index}",
                    "kind": "action",
                    "category": str(group["page"]),
                    "label": str(label),
                    "purpose": str(purpose),
                    "effect": str(effect),
                    "caution": str(caution),
                    "href": str(group["href"]),
                }
            )
    return result
