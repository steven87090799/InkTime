(() => {
  const initial = JSON.parse(document.getElementById('prompt-preview-data').textContent);
  const form = document.getElementById('scoring-profile-form');
  const editor = form?.elements.rules;
  const scope = document.getElementById('prompt-scope');
  const status = document.getElementById('prompt-preview-status');
  const system = document.getElementById('prompt-system');
  const previewButton = document.getElementById('preview-prompt');
  let inFlight = false;
  let stale = false;
  const charCount = text => Array.from(text).length;
  const markStale = () => {
    stale = true;
    document.getElementById('copy-prompt').disabled = true;
    document.getElementById('rules-count').textContent = `${charCount(editor.value.trim())} / 12,000 字元`;
    window.inktimeSetStatus(status, '編輯內容已變更；下方仍是上次預覽，請按「預覽修改」更新。', 'warning');
  };
  editor?.addEventListener('input', markStale);
  const render = data => {
    system.textContent = data.system_prompt;
    const delta = data.char_delta > 0 ? `+${data.char_delta}` : String(data.char_delta);
    document.getElementById('prompt-size').textContent = `System 文字 ${data.prompt_chars} 字元 · 評分參考 ${data.rules_chars} 字元 · 與已儲存設定相比 ${delta} 字元`;
    const formats = document.getElementById('prompt-provider-formats');
    formats.replaceChildren();
    const node = (tag, text, className = '') => {
      const element = document.createElement(tag);
      element.textContent = text;
      if (className) element.className = className;
      return element;
    };
    data.providers.forEach(provider => {
      const article = document.createElement('article');
      article.append(node('h3', `${provider.name} / ${provider.model}`));
      if (provider.response_format) {
        article.append(node('p', `Schema 封裝 ${provider.schema_chars} 字元`), node('pre', JSON.stringify(provider.response_format, null, 2), 'prompt-text'));
      } else {
        article.append(node('p', '此服務未啟用 JSON Schema；請求不附 response_format，回應仍受伺服器的 Schema v4 驗證。'));
      }
      formats.append(article);
    });
    if (!data.providers.length) formats.append(node('p', '目前沒有啟用模型服務；新增服務後會顯示對應封裝。'));
    stale = false;
    document.getElementById('copy-prompt').disabled = false;
    window.inktimeSetStatus(status, data.is_draft ? '未儲存的修改預覽；未呼叫模型。儲存為新版本後才會生效。' : '目前已儲存設定的預覽；未呼叫模型。', 'success');
  };
  const preview = async () => {
    if (inFlight) return;
    if (editor && !editor.reportValidity()) return;
    const rules = editor?.value;
    const isDraft = editor && rules.trim() !== initial.rules.trim();
    const selectedScope = scope.value;
    inFlight = true;
    scope.disabled = true;
    if (previewButton) previewButton.disabled = true;
    document.getElementById('copy-prompt').disabled = true;
    window.inktimeSetStatus(status, '正在組裝預覽…', 'info');
    try {
      const path = isDraft ? '/api/v1/scoring/prompt/preview' : '/api/v1/scoring/prompt';
      const response = await window.inktimeFetch(`${path}?scope=${encodeURIComponent(selectedScope)}`, isDraft ? {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({rules})
      } : {});
      const data = await window.inktimeDecodeJson(response);
      if (!response.ok) throw new Error(data.message || '預覽失敗');
      if (editor && editor.value !== rules) {
        markStale();
        return;
      }
      render(data);
    } catch (error) {
      stale = true;
      window.inktimeSetStatus(status, `${error?.message || '預覽失敗'}；下方保留上次預覽。`, 'error');
    } finally {
      inFlight = false;
      scope.disabled = false;
      if (previewButton) previewButton.disabled = false;
    }
  };
  previewButton?.addEventListener('click', preview);
  scope.addEventListener('change', preview);
  document.getElementById('copy-prompt').addEventListener('click', async () => {
    if (stale || inFlight) return;
    try {
      await navigator.clipboard.writeText(system.textContent);
      window.inktimeSetStatus(status, '已複製目前顯示的 System 提示詞。', 'success');
    } catch {
      window.inktimeSetStatus(status, '無法自動複製，請選取上方提示詞文字後複製。', 'error');
    }
  });
})();
