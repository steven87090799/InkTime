(() => {
  const banner = document.getElementById('analysis-failure-alert');
  const counter = document.getElementById('analysis-failure-count');
  if (!banner || !counter) return;
  async function refresh() {
    try {
      const response = await fetch('/api/v1/notifications/unread-analysis', {headers: {'Accept': 'application/json'}});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
      const result = await response.json();
      const count = Number(result.count) || 0;
      counter.textContent = String(count);
      banner.hidden = count === 0;
    } catch (_) { /* Keep the last known alert visible while disconnected. */ }
  }
  window.setInterval(() => { if (!document.hidden) refresh(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
