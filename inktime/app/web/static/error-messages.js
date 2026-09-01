/* Presentation only: do not change response status, retry policy or raw traces. */
(() => {
  'use strict';
  const config = window.inktimeErrorCatalog || {entries:{},fallback:{
    title:'暫時無法載入完整錯誤說明',detail:'錯誤說明資源尚未載入，無法確認詳細分類。',action:'確認連線並重新整理；先查看工作狀態，避免重複提交。'
  }};
  const catalog = Object.assign(Object.create(null), config.entries), fallback = config.fallback;
  const codePattern = /\b[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+\b/g;
  const chinese = /[\u3400-\u9fff]/;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  // Server-side explanations are already redacted. This also protects direct
  // browser/network exceptions and older cached responses.
  const safe = value => String(value ?? '')
    .replace(/(["'](?:api[_-]?key|x[_-]?api[_-]?key|token|password|passwd|secret|authorization|cookie|credential|access[_-]?token|refresh[_-]?token)["']\s*:\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/gi, '$1"[已遮蔽]"')
    .replace(/\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{4,}/gi, '[已遮蔽]')
    .replace(/\b(?:sk-|itd_|ids_)[A-Za-z0-9._~-]{8,}/g, '[已遮蔽]')
    .replace(/https?:\/\/[^\s<>'"]+/g, candidate => {
      try {
        const url = new URL(candidate);
        url.username = ''; url.password = '';
        [...url.searchParams.keys()].forEach(key => {
          if (/^(?:api[_-]?key|apikey|key|token|secret|signature|sig|authorization|password|passwd)$/i.test(key)) url.searchParams.set(key, '[已遮蔽]');
        });
        return url.toString();
      } catch (_) { return '[已遮蔽 URL]'; }
    })
    .replace(/\b(?:api[_-]?key|apikey|token|password|passwd|secret|authorization|cookie|session|csrf|pairing[_-]?(?:code|nonce)|device[_-]?secret)=([^\s&]+)/gi, '[已遮蔽]')
    .replace(/(?:\/Users\/|\/home\/|\/photos\/)[^\s]+/g, '[已遮蔽路徑]')
    .replace(/(?:data:image\/[^;]+;base64,|[A-Za-z0-9+/]{256,}={0,2})/g, '[已遮蔽圖片資料]')
    .replace(/(?<!\d)(?:-?\d{1,2}\.\d{4,})\s*[,，]\s*(?:-?\d{1,3}\.\d{4,})(?!\d)/g, '[已遮蔽 GPS]').slice(0, 1500);
  window.inktimePlainMessage = message => safe(message).replace(codePattern, value => catalog[value]?.title || value);
  window.inktimeExplainError = (code = '', message = '', httpStatus = null) => {
    const raw = safe(message);
    let key = safe(code || '').trim().slice(0, 128);
    const initial = raw.match(/^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+/);
    if (!key && initial && (catalog[initial[0]] || /-\d{3}$/.test(initial[0]))) key = initial[0];
    if (!key && /Failed to fetch|NetworkError|Load failed/i.test(raw)) key = 'NETWORK-ERROR';
    if (!key && httpStatus) key = `HTTP-${httpStatus}`;
    let entry = {...(catalog[key] || fallback)};
    if (key === 'VLM-008' && /截圖|禁止上傳|never_upload/.test(raw)) {
      entry = {title:'這張照片不允許傳送給模型',detail:'照片被確認為截圖，或已設定禁止上傳；這是照片的隱私保護條件，不是模型故障。',action:'到照片詳情核對本機預篩選與隱私設定；確認原因前不要重複提交。'};
    } else if (key === 'VLM-008' && raw.includes('AI 模式目前為關閉')) {
      entry = {...(catalog['ANALYSIS-DISABLED'] || fallback)};
    } else if (/no endpoints found that can handle/i.test(raw)) {
      entry = {title:'模型路由目前沒有相容端點',detail:'模型服務找不到能同時支援本次圖片與輸出格式的端點。',action:'稍後再試，或改用固定且支援圖片與結構化輸出的模型；增加 Worker 不會解除這個限制。'};
    } else if ((String(httpStatus) === '429' || /\b429\b/.test(raw)) && /^(VLM|AI-PROVIDER)/.test(key)) {
      entry = {...(catalog['VLM-002'] || fallback)};
    } else if (/^(VLM|AI-PROVIDER)/.test(key) && ['401','403'].includes(String(httpStatus))) {
      entry = {...(catalog.AUTH_REQUIRED || fallback)};
    } else if (/^(VLM|AI-PROVIDER)/.test(key) && String(httpStatus) === '402') {
      entry = {title:'模型服務的帳號額度不足',detail:'服務端拒絕這次請求，回報需要額度或付款；這不是背景工作數量不足。',action:'核對服務端帳戶額度及實際選用模型；免費路由也需遵守服務端限制。'};
    } else if (/^(VLM|AI-PROVIDER)/.test(key) && ['500','502','503','504'].includes(String(httpStatus))) {
      entry = {title:'模型服務端暫時發生故障',detail:'上游服務回報內部錯誤或逾時，暫時無法取得有效分析結果。',action:'先查看呼叫紀錄與遠端使用量；等待冷卻或更換可用模型，避免連續重送。'};
    }
    const specific = (key ? raw.split(key).join('') : raw)
      .replace(codePattern, value => catalog[value]?.title || (value === 'SHA-256' ? value : '相關檢查')).replace(/^[ ：:｜|—\-\n]+|[ ：:｜|—\-\n]+$/g, '');
    if (!key && chinese.test(specific)) entry.title = '操作未完成';
    if (chinese.test(specific) && !/Traceback|[<>]|\{|\\n/.test(specific) && ![entry.title, entry.detail].includes(specific)) entry.detail += ' 本次回報：' + specific.slice(0, 500);
    return {...entry,code:key,technical_message:raw,known:Boolean(catalog[key]),message:[entry.title,entry.detail,entry.action].map(value=>value.replace(/。$/, '')).join('。')+'。'};
  };
  window.inktimeErrorText = (code = '', message = '', status = null) => window.inktimeExplainError(code, message, status).message;
  window.inktimeErrorHtml = (code = '', message = '', status = null) => {
    if (!code && !message && !status) return '<span class="muted">沒有錯誤</span>';
    const e = window.inktimeExplainError(code, message, status);
    return `<div class="error-explanation"><strong class="error-explanation-title">${esc(e.title)}</strong><p>${esc(e.detail)}</p><p class="error-next-step"><span>可以怎麼處理：</span>${esc(e.action)}</p><details class="error-technical"><summary>技術資料（供查詢紀錄）</summary>${e.code?`<p>識別代碼：<code>${esc(e.code)}</code></p>`:''}${e.technical_message?`<pre>${esc(e.technical_message)}</pre>`:''}</details></div>`;
  };
  window.inktimeDecodeJson = async response => {
    let body;
    try {
      const text = await response.text();
      body = text ? JSON.parse(text) : {};
    } catch (_) {
      const explanation = window.inktimeExplainError('RESPONSE-INVALID', '', response.status);
      if (response.ok) throw new Error(explanation.message);
      return {message:explanation.message,user_error:explanation};
    }
    if (!response.ok && (!body || typeof body !== 'object' || Array.isArray(body))) {
      const explanation = window.inktimeExplainError('', '', response.status);
      return {message:explanation.message,user_error:explanation};
    }
    if (!response.ok && body && typeof body === 'object' && !Array.isArray(body)) {
      const nested = body.error && typeof body.error === 'object' ? body.error : {};
      const explanation = body.user_error || window.inktimeExplainError(body.error_code || nested.code, body.message || nested.message || (typeof body.error==='string'?body.error:''), response.status);
      // Only the browser's presentation copy changes. APIs retain machine codes.
      return {...body,message:explanation.message,user_error:explanation};
    }
    return body;
  };
})();
