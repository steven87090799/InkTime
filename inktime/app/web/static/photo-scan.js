(() => {
  const refresh = document.getElementById('photo-scan-refresh');
  if (refresh) refresh.onclick = () => location.reload();
  const scanForm = document.getElementById('scan-form');
  if (scanForm) scanForm.onsubmit = event => {
    event.preventDefault();
    const button = scanForm.querySelector('button');
    return window.inktimeRunAction({button, status: '#scan-status', unknownMessage: '掃描建立結果尚未確認，請先查看工作列表，避免重複建立。'}, async () => {
      const form = new FormData(scanForm);
      window.inktimeSetStatus('#scan-status', '正在建立掃描工作…');
      const response = await window.inktimeFetch('/api/v1/maintenance/scan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({library_name: form.get('library_name'), root_path: form.get('root_path'), mode: form.get('mode'), build_thumbnails: form.has('build_thumbnails')})
      });
      const data = await window.inktimeDecodeJson(response);
      if (!response.ok) throw new Error(data.message || '無法建立掃描工作');
      if (typeof data.detail_url !== 'string' || !/^\/jobs\/[a-zA-Z0-9-]+$/.test(data.detail_url)) throw new Error('工作回覆缺少可核對的進度連結，請查看工作列表。');
      location.href = data.detail_url;
    });
  };
  const scheduleForm = document.getElementById('photo-scan-schedule');
  if (!scheduleForm) return;
  const frequency = scheduleForm.elements.frequency;
  const clock = scheduleForm.elements.time;
  const sync = () => { clock.disabled = ['manual', 'custom'].includes(frequency.value); };
  frequency.onchange = sync;
  sync();
  scheduleForm.onsubmit = event => {
    event.preventDefault();
    const selected = frequency.value;
    if (selected === 'custom') {
      window.inktimeSetStatus('#scan-schedule-status', '已保留自訂排程。請至增量掃描進階排程調整 Cron 與允許時段。');
      return;
    }
    const tails = {daily: '* * *', weekly: '* * 1', monthly: '1 * *'};
    let body = {enabled: false};
    if (selected !== 'manual') {
      if (!tails[selected] || !/^\d{2}:\d{2}$/.test(clock.value)) return;
      const [hour, minute] = clock.value.split(':').map(Number);
      body = {enabled: true, cron: `${minute} ${hour} ${tails[selected]}`, start_time: clock.value, weekdays: [], window_start: null, window_end: null};
    }
    return window.inktimeRunAction({button: scheduleForm.querySelector('button'), status: '#scan-schedule-status', unknownMessage: '儲存結果尚未確認，請重新整理頁面核對目前頻率。'}, async () => {
      const response = await window.inktimeFetch('/api/v1/schedules/incremental_scan', {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const data = await window.inktimeDecodeJson(response);
      if (!response.ok) throw new Error(data.message || '無法儲存掃描頻率');
      location.reload();
    });
  };
})();
