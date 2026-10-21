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
      card.querySelectorAll('[data-action]').forEach(button => {
        button.disabled = busy.has(card.dataset.id) || button.dataset.requiresOrientation === '1';
      });
    });
    if (!focus) return;
    const input = [...grid.querySelectorAll('.review-card')].find(card => card.dataset.id === focus.id)?.querySelector('[data-caption]');
    if (input) { input.focus({preventScroll: true}); input.setSelectionRange(focus.start, focus.end); }
  };
  const draw = () => {
    const focus = capture();
    grid.innerHTML = items.map((p, i) => {
      const scores = p.score_kind === 'local_quality'
        ? `本機品質 ${p.local_quality_score ?? '—'}／AI 語意評分：尚未分析`
        : `一般回看價值 ${p.memory_score ?? '—'}／視覺 ${p.visual_score ?? '—'}／本機品質 ${p.local_quality_score ?? '—'}／綜合 ${p.ranking_score ?? '—'}`;
      const rotation = [0, 90, 180, 270].includes(Number(p.effective_visual_orientation))
        ? Number(p.effective_visual_orientation) : 0;
      const scale = rotation === 90 || rotation === 270 ? 0.8 : 1;
      const sourceLabel = {manual: '人工設定', ai: 'AI 判斷', exif_normalized: 'EXIF 已正規化', none: '無額外旋轉'}[p.orientation_source] || '未知';
      const aiAngle = p.visual_orientation_rotation_cw == null ? '未知' : `${p.visual_orientation_rotation_cw}°`;
      const aiConfidence = p.visual_orientation_confidence == null
        ? '—' : `${Math.round(Number(p.visual_orientation_confidence) * 100)}%`;
      const reasonLabels = {
        adult_media_cover: '成人影片或成人媒體封面', commercial_model_photo: '商業模特照',
        screenshot: '截圖', document_record: '文件或紀錄照',
        document_or_receipt: '文件或收據', REVIEW_PENDING: '等待人工判斷', REVIEW_EXCLUDED: '已人工排除',
        sexualized_content: '色情化內容', explicit_nudity: '裸露內容',
        female_glamour_portrait: '女性寫真', orientation_unverified: '照片方向待確認',
        display_risk_uncertain: '顯示內容分類待確認'
      };
      const reasonCode = p.reject_reason || p.reject_rule || '';
      const reason = reasonLabels[reasonCode] || reasonCode || '沒有排除理由';
      const riskConfidence = Number(p.reject_confidence);
      const confidenceText = p.reject_confidence != null && Number.isFinite(riskConfidence)
        ? `；判斷信心 ${Math.round(riskConfidence * 100)}%` : '';
      const held = p.exclusion_status === 'pending_review' || !p.eligible
        || ['auto_excluded', 'manually_excluded'].includes(p.exclusion_status);
      const releaseLabel = p.exclusion_status === 'pending_review' ? '待人工確認，不送相框'
        : held ? '目前已排除，不送相框' : '目前未排除，發布時仍須通過資格檢查';
      const orientationNeedsConfirmation = p.orientation_review_required
        && p.manual_orientation_rotation_cw == null;
      const rotationButtons = [0, 90, 180, 270].map(degrees =>
        `<button type="button" data-action="rotate:${degrees}" aria-pressed="${p.manual_orientation_rotation_cw === degrees}">${degrees}°</button>`
      ).join('');
      return `<article class="review-card${i === selected ? ' selected' : ''}" data-index="${i}" data-id="${escapeHtml(p.id)}">
        <div class="review-image-stage"><img loading="lazy" style="--review-rotation:${rotation}deg;--review-scale:${scale}" src="${escapeHtml(p.thumbnail_url)}" alt="照片縮圖，額外順時針旋轉 ${rotation} 度"></div>
        <div class="review-card-body">
          <span class="review-release-status${held ? ' is-held' : ''}" role="status">${releaseLabel}</span>
          <strong>${escapeHtml(p.caption_override || p.side_caption || '尚未有短文案')}</strong>
          <small>${escapeHtml(p.review_state)} · ${escapeHtml(p.review_taken_at || '日期未知')} · ${p.analysis_id ? '已有分析' : '尚無分析'} · ${escapeHtml(p.analysis_provider || '—')}／${escapeHtml(p.analysis_model || '—')}</small>
          <small>${scores} · 內容分類最低信心 ${escapeHtml(p.confidence ?? '—')}</small>
          <small>${escapeHtml(reason)}${confidenceText} · ${p.low_confidence ? '低信心' : ''}</small>
          <small>相框前額外旋轉：${rotation}°（${sourceLabel}）；AI 建議 ${escapeHtml(aiAngle)}／${escapeHtml(aiConfidence)}${p.visual_orientation_ambiguous ? '，方向不明' : ''}</small>
          ${p.review_state === 'needs_review' ? '<small role="status">待確認：目前不會送往相框。</small>' : ''}
          ${orientationNeedsConfirmation ? '<small role="status">請先選擇並儲存方向，再確認可顯示。</small>' : ''}
          <div class="review-actions" role="group" aria-label="人工方向校正">${rotationButtons}</div>
          <a href="/photos/${encodeURIComponent(p.id)}">查看照片詳細資料</a>
          <label>人工短文案<textarea data-caption maxlength="1000">${escapeHtml(drafts.has(p.id) ? drafts.get(p.id) : (p.caption_override || ''))}</textarea></label>
          <div class="review-actions">
            <button type="button" data-action="keep" data-requires-orientation="${orientationNeedsConfirmation ? '1' : '0'}">確認可顯示</button>
            <button type="button" data-action="exclude">排除且不顯示</button>
            <button type="button" data-action="needs_review">維持待確認</button>
            ${p.review_state === 'needs_review' ? '' : `<button type="button" data-action="candidate">${p.candidate_pool ? '移出候選' : '加入候選'}</button>`}
            <button type="button" data-action="favorite">${p.favorite ? '移除最愛' : '加入最愛'}</button>
            <button type="button" data-action="understanding_incorrect">${p.understanding_incorrect ? '清除理解錯誤' : '理解錯誤'}</button>
            <button type="button" data-action="caption_bad">${p.caption_bad ? '清除文案問題' : '文案不好'}</button>
            <button type="button" data-action="scores_unreasonable">${p.scores_unreasonable ? '清除分數問題' : '分數不合理'}</button>
            <button type="button" data-action="save_caption">保存短文案</button>
          </div>
        </div>
      </article>`;
    }).join('');
    if (!items.length) grid.innerHTML = '<p class="empty">目前沒有符合條件的待確認照片。</p>';
    grid.querySelectorAll('.review-card').forEach(card => card.addEventListener('click', event => {
      if (event.target.closest('input,textarea,select,button,a')) return;
      select(Number(card.dataset.index));
    }));
    grid.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', event => {
      event.stopPropagation();
      update(button.closest('.review-card').dataset.index, button.dataset.action);
    }));
    restore(focus);
  };
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
    if (action.startsWith('rotate:')) {
      const rotation = Number(action.slice('rotate:'.length));
      if (![0, 90, 180, 270].includes(rotation)) return;
      busy.add(photo.id); restore(null);
      try {
        const response = await window.inktimeFetch('/api/v1/photos/' + encodeURIComponent(photo.id) + '/orientation', {
          method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({rotation_cw: rotation})
        });
        const data = await window.inktimeDecodeJson(response);
        if (!response.ok || ![0, 90, 180, 270].includes(data.rotation_degrees)) {
          throw Error(data.message || '方向儲存失敗');
        }
        const position = items.findIndex(item => item === photo);
        if (position >= 0) {
          items[position] = {...photo, manual_orientation_rotation_cw: rotation,
            effective_visual_orientation: data.rotation_degrees, orientation_source: data.source};
        }
        status.textContent = '已儲存方向 ' + rotation + '°；仍須選擇確認可顯示或排除。';
        draw();
      } catch (error) {
        status.textContent = (error.message || '方向儲存失敗') + '；請重新載入查核。';
      } finally {
        busy.delete(photo.id); restore(null);
      }
      return;
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
      if (applied && ['keep', 'exclude'].includes(action) && form.elements.review_state.value === 'needs_review') {
        items.splice(position, 1);
        selected = Math.min(selected, items.length - 1);
        summary.textContent = '待確認清單已更新；重新套用可更新總數。';
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
