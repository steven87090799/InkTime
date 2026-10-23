(() => {
  'use strict';
  const toggle = document.getElementById('report-show-all');
  const full = document.getElementById('report-all-settings');
  toggle.addEventListener('change', () => {
    full.hidden = !toggle.checked;
    toggle.setAttribute('aria-expanded', String(toggle.checked));
  });
  const entries = [...full.querySelectorAll('.report-setting')];
  document.getElementById('report-search').addEventListener('input', event => {
    const query = event.target.value.trim().toLocaleLowerCase('zh-Hant-TW');
    let count = 0;
    for (const entry of entries) {
      entry.hidden = !entry.textContent.toLocaleLowerCase('zh-Hant-TW').includes(query);
      if (!entry.hidden) count += 1;
    }
    for (const section of full.querySelectorAll('.report-category')) {
      section.hidden = ![...section.querySelectorAll('.report-setting')].some(entry => !entry.hidden);
    }
    document.getElementById('report-match-count').textContent = `顯示 ${count}／${entries.length} 項設定`;
  });
  document.getElementById('report-print').addEventListener('click', () => window.print());
})();
