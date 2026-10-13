(() => {
  const form = document.getElementById('review-filter');
  const grid = document.getElementById('review-grid');
  const more = document.getElementById('review-more');
  const summary = document.getElementById('review-summary');
  const status = document.getElementById('review-status');
  const drafts = new Map(), busy = new Set();
  let cursor = null, selected = -1, items = [], loading = false, generation = 0, controller;
  const params = () => new URLSearchParams([...new FormData(form).entries()].filter(([, value]) => value));
  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const select = index => {
    selected = index;
    grid.querySelectorAll('.review-card').forEach(card => card.classList.toggle('selected', Number(card.dataset.index) === selected));
  };
  const capture = () => {
    const active = document.activeElement;
    if (!active?.matches('[data-caption]') || !grid.contains(active)) return null;
    return {id: active.closest('.review-card').dataset.id, start: active.selectionStart, end: active.selectionEnd};
  };
  const restore = focus => {
    grid.querySelectorAll('.review-card').forEach(card => {
      card.querySelectorAll('[data-action]').forEach(button => { button.disabled = busy.has(card.dataset.id); });
    });
    if (!focus) return;
    const input = [...grid.querySelectorAll('.review-card')].find(card => card.dataset.id === focus.id)?.querySelector('[data-caption]');
    if (input) { input.focus({preventScroll: true}); input.setSelectionRange(focus.start, focus.end); }
  };
  const draw=()=>{const focus=capture();grid.innerHTML=items.map((p,i)=>{const scores=p.score_kind==='local_quality'?`本機品質 ${p.local_quality_score??'—'}／AI 語意評分：尚未分析`:`一般回看價值 ${p.memory_score??'—'}／視覺 ${p.visual_score??'—'}／本機品質 ${p.local_quality_score??'—'}／綜合 ${p.ranking_score??'—'}`;return `<article class="review-card${i===selected?' selected':''}" data-index="${i}" data-id="${escapeHtml(p.id)}"><img loading="lazy" src="${escapeHtml(p.thumbnail_url)}" alt="${escapeHtml(p.side_caption||'照片縮圖')}"><div class="review-card-body"><strong>${escapeHtml(p.caption_override||p.side_caption||'尚未有短文案')}</strong><small>${escapeHtml(p.review_state)} · ${escapeHtml(p.review_taken_at||'日期未知')} · ${p.analysis_id?'已有分析':'尚無分析'} · ${escapeHtml(p.analysis_provider||'—')}／${escapeHtml(p.analysis_model||'—')}</small><small>${scores} · 內容分類最低信心 ${escapeHtml(p.confidence??'—')}</small><small>${escapeHtml(p.reject_reason||p.reject_rule||'沒有排除理由')} · ${p.low_confidence?'低信心':''}</small>${p.review_state==='needs_review'?'<small role="status">待確認狀態由系統自動管理候選池。</small>':''}<label>人工短文案<textarea data-caption maxlength="1000">${escapeHtml(drafts.has(p.id)?drafts.get(p.id):(p.caption_override||''))}</textarea></label><div class="review-actions"><button data-action="keep">接受</button><button data-action="exclude">排除</button><button data-action="needs_review">待確認</button>${p.review_state==='needs_review'?'':`<button data-action="candidate">${p.candidate_pool?'移出候選':'加入候選'}</button>`}<button data-action="favorite">${p.favorite?'移除最愛':'加入最愛'}</button><button data-action="understanding_incorrect">${p.understanding_incorrect?'清除理解錯誤':'理解錯誤'}</button><button data-action="caption_bad">${p.caption_bad?'清除文案問題':'文案不好'}</button><button data-action="scores_unreasonable">${p.scores_unreasonable?'清除分數問題':'分數不合理'}</button><button data-action="save_caption">保存短文案</button></div></div></article>`;}).join(''); grid.querySelectorAll('.review-card').forEach(card=>card.addEventListener('click',event=>{if(event.target.closest('input,textarea,select,button,a'))return;select(Number(card.dataset.index));})); grid.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',event=>{event.stopPropagation(); update(button.closest('.review-card').dataset.index,button.dataset.action);}));restore(focus);};
  grid.addEventListener('input', event => {
    if (event.target.matches('[data-caption]')) drafts.set(event.target.closest('.review-card').dataset.id, event.target.value);
  });
  grid.addEventListener('error', event => {
    if (event.target?.tagName !== 'IMG') return;
    const placeholder = document.createElement('div');
    placeholder.className = 'review-image-error'; placeholder.setAttribute('role', 'status');
    placeholder.textContent = '縮圖無法載入。'; event.target.replaceWith(placeholder);
  }, true);
  const load = async append => {
    if (append && loading) return;
    capture();
    controller?.abort(); controller = new AbortController();
    const signal = controller.signal, current = ++generation;
    const baseQuery = params(), query = new URLSearchParams(baseQuery);
    if (append && cursor) query.set('cursor', cursor);
    loading = true; more.disabled = true;
    const submit = form.querySelector('[type="submit"],button:not([type])');
    if (submit) submit.disabled = true;
    status.textContent = '讀取中…';
    try {
      const response = await window.inktimeFetch('/api/v1/review/photos?' + query, {signal});
      const data = await window.inktimeDecodeJson(response);
      if (!response.ok || !Array.isArray(data.items)) throw Error(data.message || '讀取失敗');
      if (current !== generation) return;
      const previous = append ? items : [];
      const known = new Set(previous.map(photo => photo.id));
      items = previous.concat(data.items.filter(photo => !known.has(photo.id)));
      if (!append) selected = -1;
      cursor = data.next_cursor; more.hidden = !data.has_more;
      history.replaceState(null, '', '?' + baseQuery);
      draw(); status.textContent = ''; summary.textContent = '摘要讀取中…';
      const responseSummary = await window.inktimeFetch('/api/v1/review/summary?' + baseQuery, {signal});
      const dataSummary = await window.inktimeDecodeJson(responseSummary);
      if (!responseSummary.ok) throw Error(dataSummary.message || '摘要讀取失敗');
      if (current !== generation) return;
      summary.textContent = `總數 ${dataSummary.total ?? 0}｜` + Object.entries(dataSummary.states || {}).map(([key, value]) => `${key} ${value}`).join('｜');
    } catch (error) {
      if (current === generation && !append && error.name !== 'AbortError') more.hidden = true;
      if (current === generation && error.name !== 'AbortError') status.textContent = `${error.message || '連線失敗'}；草稿保留，請重新載入查核。`;
    } finally {
      if (current === generation) {
        loading = false; more.disabled = false;
        if (submit) submit.disabled = false;
      }
    }
  };
  const update = async (index, action) => {
    const photo = items[Number(index)];
    if (!photo || busy.has(photo.id)) return;
    if (action === 'candidate' && photo.review_state === 'needs_review') {
      status.textContent = '待確認狀態由系統自動管理候選池。'; return;
    }
    const card = grid.querySelector(`.review-card[data-index="${Number(index)}"]`);
    const payload = {expected_version: photo.review_version, analysis_id: photo.analysis_id ?? null};
    if (action === 'candidate') payload.candidate_pool = !photo.candidate_pool;
    else if (action === 'favorite') payload.favorite = !photo.favorite;
    else if (['understanding_incorrect', 'caption_bad', 'scores_unreasonable'].includes(action)) payload[action] = !photo[action];
    else if (action === 'save_caption') payload.caption_override = card?.querySelector('[data-caption]')?.value ?? '';
    else payload.review_state = action;
    capture(); busy.add(photo.id); restore(null);
    try {
      const response = await window.inktimeFetch('/api/v1/review/photos/' + encodeURIComponent(photo.id), {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
      });
      const data = await window.inktimeDecodeJson(response);
      if (!response.ok || !data.photo) throw Error(data.message || '寫入失敗，請重新載入查核');
      const position = items.findIndex(item => item.id === photo.id);
      const applied = position >= 0 && items[position].review_version === photo.review_version && items[position].analysis_id === photo.analysis_id;
      if (applied) {
        items[position] = data.photo;
      }
      if (applied && action === 'save_caption' && drafts.get(photo.id) === payload.caption_override) {
        drafts.delete(photo.id);
        const currentCard = [...grid.querySelectorAll('.review-card')].find(value => value.dataset.id === photo.id);
        const input = currentCard?.querySelector('[data-caption]');
        if (input && input.value === payload.caption_override) input.value = data.photo.caption_override || '';
      }
      status.textContent = '已儲存 ' + photo.id; draw();
    } catch (error) {
      status.textContent = `${error.message || '連線失敗'}；儲存結果請重新載入查核，草稿保留。`;
    } finally {
      busy.delete(photo.id); restore(null);
    }
  };
  [...form.elements].forEach(element => {
    const value = new URLSearchParams(location.search).get(element.name);
    if (value !== null) element.value = value;
  });
  form.addEventListener('submit', event => { event.preventDefault(); load(false); });
  more.addEventListener('click', () => load(true));
  document.addEventListener('keydown', event => {
    if (document.activeElement?.closest('input,select,textarea,button,a,[contenteditable="true"]')) return;
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault(); select(Math.max(0, Math.min(items.length - 1, selected + (event.key === 'ArrowRight' ? 1 : -1))));
    } else if (selected >= 0) {
      const action = {k:'keep', x:'exclude', n:'needs_review', f:'candidate'}[event.key.toLowerCase()];
      if (action) { event.preventDefault(); update(selected, action); }
    }
  });
  load(false);
})();
