(() => {
  'use strict';

  const config = JSON.parse(document.getElementById('receiver-config').textContent);
  const pollMilliseconds = Math.max(1000, Number(config.pollSeconds || 5) * 1000);
  const startedAt = performance.now();
  const state = {
    polls: 0,
    releases: 0,
    frames: 0,
    bytes: 0,
    verifications: 0,
    errors: 0,
    lastFrameKey: '',
    lastReleaseId: '',
    lastLogKey: '',
    manifest: null,
    frameIndex: 0,
    switching: false,
  };

  const elements = {
    connection: document.getElementById('connection-state'),
    device: document.getElementById('display-device'),
    canvas: document.getElementById('display-canvas'),
    placeholder: document.getElementById('display-placeholder'),
    framePosition: document.getElementById('frame-position'),
    frameControlStatus: document.getElementById('frame-control-status'),
    previousFrame: document.getElementById('previous-frame'),
    nextFrame: document.getElementById('next-frame'),
    verification: document.getElementById('verification-badge'),
    palette: document.getElementById('palette-distribution'),
    paletteTotal: document.getElementById('palette-total'),
    debug: document.getElementById('debug-stream'),
    meta: {
      release: document.getElementById('meta-release'),
      file: document.getElementById('meta-file'),
      profile: document.getElementById('meta-profile'),
      format: document.getElementById('meta-format'),
      dither: document.getElementById('meta-dither'),
      created: document.getElementById('meta-created'),
      received: document.getElementById('meta-received'),
      sha: document.getElementById('meta-sha'),
    },
    stats: {
      uptime: document.getElementById('stat-uptime'),
      polls: document.getElementById('stat-polls'),
      releases: document.getElementById('stat-releases'),
      frames: document.getElementById('stat-frames'),
      bytes: document.getElementById('stat-bytes'),
      verifications: document.getElementById('stat-verifications'),
      latency: document.getElementById('stat-latency'),
      errors: document.getElementById('stat-errors'),
    },
  };

  const numberFormatter = new Intl.NumberFormat('zh-TW');
  const timeFormatter = new Intl.DateTimeFormat('zh-TW', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  const dateFormatter = new Intl.DateTimeFormat('zh-TW', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  const ditherLabels = {
    gooddisplay: 'Good Display 原廠相容',
    photo_smooth: '照片平滑',
    floyd_steinberg: 'Floyd–Steinberg（InkTime）',
    atkinson: 'Atkinson',
    bayer4: 'Bayer 4×4',
    bayer8: 'Bayer 8×8',
    none: '不抖動',
  };

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / 1024 ** 2).toFixed(2)} MiB`;
  }

  function formatDuration(milliseconds) {
    const seconds = Math.max(0, Math.floor(milliseconds / 1000));
    const hours = String(Math.floor(seconds / 3600)).padStart(2, '0');
    const minutes = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
    return `${hours}:${minutes}:${String(seconds % 60).padStart(2, '0')}`;
  }

  function updateStats() {
    elements.stats.uptime.textContent = formatDuration(performance.now() - startedAt);
    elements.stats.polls.textContent = numberFormatter.format(state.polls);
    elements.stats.releases.textContent = numberFormatter.format(state.releases);
    elements.stats.frames.textContent = numberFormatter.format(state.frames);
    elements.stats.bytes.textContent = formatBytes(state.bytes);
    elements.stats.verifications.textContent = numberFormatter.format(state.verifications);
    elements.stats.errors.textContent = numberFormatter.format(state.errors);
  }

  function setConnection(mode, label) {
    elements.connection.className = `connection-state ${mode}`;
    elements.connection.querySelector('span').textContent = label;
  }

  function debug(level, message, key = '') {
    const dedupeKey = key || `${level}:${message}`;
    if (dedupeKey === state.lastLogKey) return;
    state.lastLogKey = dedupeKey;
    const item = document.createElement('li');
    const time = document.createElement('span');
    const levelNode = document.createElement('span');
    const detail = document.createElement('span');
    time.className = 'debug-time';
    levelNode.className = `debug-level ${level}`;
    detail.className = 'debug-message';
    time.textContent = timeFormatter.format(new Date());
    levelNode.textContent = level === 'error' ? 'ERROR' : level === 'warn' ? 'WARN' : 'INFO';
    detail.textContent = message;
    item.append(time, levelNode, detail);
    elements.debug.prepend(item);
    while (elements.debug.children.length > 80) elements.debug.lastElementChild.remove();
  }

  async function responseMessage(response) {
    try {
      const payload = await response.json();
      return payload.message || payload.error_code || `HTTP ${response.status}`;
    } catch (_error) {
      return `HTTP ${response.status}`;
    }
  }

  function bytesToHex(buffer) {
    return Array.from(new Uint8Array(buffer), byte => byte.toString(16).padStart(2, '0')).join('');
  }

  async function verifyPayload(buffer, expectedSha, serverSha) {
    const expected = String(expectedSha || '').toLowerCase();
    let actual = String(serverSha || '').toLowerCase();
    let method = '伺服器 SHA-256';
    if (globalThis.crypto && globalThis.crypto.subtle) {
      actual = bytesToHex(await globalThis.crypto.subtle.digest('SHA-256', buffer));
      method = '瀏覽器 SHA-256';
    }
    if (!expected || !actual || actual !== expected) {
      throw new Error(`SHA-256 不一致：expected=${expected || 'missing'} actual=${actual || 'missing'}`);
    }
    return {actual, method};
  }

  function decodeFrame(buffer, manifest) {
    const width = Number(manifest.width);
    const height = Number(manifest.height);
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 || width * height > 2_000_000) {
      throw new Error('Manifest 畫面尺寸不合法');
    }
    if (!['2bpp', 'indexed4'].includes(manifest.pixel_format)) {
      throw new Error(`不支援的 Pixel Format：${manifest.pixel_format}`);
    }
    const bytes = new Uint8Array(buffer);
    const pixelCount = width * height;
    const expectedBytes = Math.ceil(pixelCount / (manifest.pixel_format === '2bpp' ? 4 : 2));
    if (bytes.length !== expectedBytes) {
      throw new Error(`Payload 大小錯誤：${bytes.length} / ${expectedBytes} bytes`);
    }
    const palette = new Map();
    for (const color of manifest.palette || []) {
      if (!Array.isArray(color.rgb) || color.rgb.length !== 3) throw new Error('Manifest 色盤格式不合法');
      palette.set(Number(color.code), color);
    }
    const image = new ImageData(width, height);
    const counts = new Map();
    for (let pixel = 0; pixel < pixelCount; pixel += 1) {
      const packed = bytes[manifest.pixel_format === '2bpp' ? Math.floor(pixel / 4) : Math.floor(pixel / 2)];
      const code = manifest.pixel_format === '2bpp'
        ? (packed >> (6 - (pixel % 4) * 2)) & 0x03
        : (packed >> (pixel % 2 === 0 ? 4 : 0)) & 0x0f;
      const color = palette.get(code);
      if (!color) throw new Error(`Payload 使用未定義的色盤 Code：${code}`);
      const offset = pixel * 4;
      image.data[offset] = Number(color.rgb[0]);
      image.data[offset + 1] = Number(color.rgb[1]);
      image.data[offset + 2] = Number(color.rgb[2]);
      image.data[offset + 3] = 255;
      counts.set(code, (counts.get(code) || 0) + 1);
    }
    elements.canvas.width = width;
    elements.canvas.height = height;
    elements.canvas.getContext('2d', {alpha: false}).putImageData(image, 0, 0);
    return {counts, palette, pixelCount};
  }

  function renderPalette({counts, palette, pixelCount}) {
    elements.palette.replaceChildren();
    for (const [code, color] of palette) {
      const count = counts.get(code) || 0;
      const percentage = count / pixelCount * 100;
      const row = document.createElement('div');
      row.className = 'palette-row';
      const label = document.createElement('span');
      const swatch = document.createElement('i');
      const track = document.createElement('span');
      const fill = document.createElement('i');
      const value = document.createElement('span');
      label.className = 'palette-name';
      swatch.className = 'palette-swatch';
      track.className = 'palette-track';
      value.className = 'palette-value';
      swatch.style.background = `rgb(${color.rgb.join(',')})`;
      fill.style.width = `${percentage}%`;
      fill.style.background = `rgb(${color.rgb.join(',')})`;
      label.append(swatch, document.createTextNode(color.name));
      track.append(fill);
      value.textContent = `${percentage.toFixed(1)}%`;
      row.append(label, track, value);
      elements.palette.append(row);
    }
    elements.paletteTotal.textContent = `${numberFormatter.format(pixelCount)} px`;
  }

  function updateNavigation() {
    const total = Array.isArray(state.manifest?.files) ? state.manifest.files.length : 0;
    const canNavigate = total > 1 && !state.switching;
    elements.previousFrame.disabled = !canNavigate;
    elements.nextFrame.disabled = !canNavigate;
    elements.frameControlStatus.textContent = total
      ? `第 ${state.frameIndex + 1} / ${total} 張`
      : '等待 Release';
  }

  function updateReceipt(manifest, file, verification, frameIndex) {
    elements.meta.release.textContent = manifest.release_id;
    elements.meta.file.textContent = file.name;
    elements.meta.profile.textContent = manifest.render_profile;
    elements.meta.format.textContent = `${manifest.width}×${manifest.height} ${manifest.pixel_format}`;
    elements.meta.dither.textContent = ditherLabels[manifest.dither] || manifest.dither || '未標示';
    elements.meta.created.textContent = dateFormatter.format(new Date(manifest.created_at));
    elements.meta.received.textContent = dateFormatter.format(new Date());
    elements.meta.sha.textContent = verification.actual;
    elements.verification.textContent = `${verification.method} 已通過`;
    elements.verification.classList.add('verified');
    elements.framePosition.textContent = `第 ${frameIndex + 1} / ${manifest.files.length} 張 · ${formatBytes(file.size)}`;
  }

  async function displayFrame(manifest, requestedIndex, {newRelease = false} = {}) {
    const files = Array.isArray(manifest.files) ? manifest.files : [];
    if (!files.length) throw new Error('Manifest 沒有可接收的檔案');
    if (state.switching) return;
    const frameIndex = ((requestedIndex % files.length) + files.length) % files.length;
    const file = files[frameIndex];
    if (!file || !file.name || !file.sha256) throw new Error('Manifest 畫面檔案不完整');
    state.switching = true;
    state.manifest = manifest;
    state.frameIndex = frameIndex;
    updateNavigation();
    setConnection('receiving', '接收中');
    elements.device.classList.add('refreshing');
    debug('info', `準備下載第 ${frameIndex + 1} 張：${file.name}。`);
    try {
      const fileUrl = new URL(`${manifest.download_base_url}${encodeURIComponent(file.name)}`, location.origin);
      const payloadResponse = await fetch(fileUrl, {credentials: 'same-origin', cache: 'no-store'});
      if (!payloadResponse.ok) throw new Error(await responseMessage(payloadResponse));
      const buffer = await payloadResponse.arrayBuffer();
      if (buffer.byteLength !== Number(file.size)) {
        throw new Error(`下載大小不符：${buffer.byteLength} / ${file.size} bytes`);
      }
      const verification = await verifyPayload(
        buffer,
        file.sha256,
        payloadResponse.headers.get('X-InkTime-Payload-SHA256'),
      );
      const decoded = decodeFrame(buffer, manifest);
      renderPalette(decoded);
      updateReceipt(manifest, file, verification, frameIndex);
      if (newRelease) state.releases += 1;
      state.lastReleaseId = manifest.release_id;
      state.lastFrameKey = `${manifest.release_id}:${file.name}:${file.sha256}`;
      state.frames += 1;
      state.bytes += buffer.byteLength;
      state.verifications += 1;
      elements.placeholder.hidden = true;
      setConnection('synced', '已同步');
      updateStats();
      debug('info', `第 ${frameIndex + 1} 張 BIN、SHA-256 與 ${manifest.pixel_format} 解碼完成。`);
    } finally {
      state.switching = false;
      updateNavigation();
      window.setTimeout(() => elements.device.classList.remove('refreshing'), 760);
    }
  }

  async function moveFrame(offset) {
    if (!state.manifest || state.switching || state.manifest.files.length < 2) return;
    try {
      await displayFrame(state.manifest, state.frameIndex + offset);
    } catch (error) {
      state.errors += 1;
      setConnection('error', '切換失敗');
      updateStats();
      debug('error', error instanceof Error ? error.message : String(error));
    }
  }

  async function receive() {
    const pollStarted = performance.now();
    state.polls += 1;
    updateStats();
    try {
      const manifestResponse = await fetch(config.manifestUrl, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Accept': 'application/json'},
      });
      if (manifestResponse.status === 404) {
        setConnection('waiting', '等待發布');
        debug('warn', '尚無可接收 Release；接收器保持輪詢。', 'waiting-release');
        return;
      }
      if (!manifestResponse.ok) throw new Error(await responseMessage(manifestResponse));
      const manifest = await manifestResponse.json();
      if (manifest.release_id === state.lastReleaseId) {
        setConnection('synced', '已同步');
        elements.stats.latency.textContent = `${Math.round(performance.now() - pollStarted)} ms`;
        debug('info', `Release ${manifest.release_id} 沒有變更。`, `unchanged:${manifest.release_id}`);
        return;
      }
      debug('info', `收到新 Manifest ${manifest.release_id}，從第 1 張開始顯示。`);
      await displayFrame(manifest, 0, {newRelease: true});
      elements.stats.latency.textContent = `${Math.round(performance.now() - pollStarted)} ms`;
    } catch (error) {
      state.errors += 1;
      setConnection('error', '接收錯誤');
      updateStats();
      debug('error', error instanceof Error ? error.message : String(error));
    } finally {
      window.setTimeout(receive, pollMilliseconds);
    }
  }

  elements.previousFrame.addEventListener('click', () => moveFrame(-1));
  elements.nextFrame.addEventListener('click', () => moveFrame(1));
  document.addEventListener('keydown', event => {
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement || event.target instanceof HTMLTextAreaElement) return;
    if (event.key === 'ArrowLeft') moveFrame(-1);
    if (event.key === 'ArrowRight') moveFrame(1);
  });
  debug('info', `唯讀接收器已啟動；Profile=${config.profile.key}，每 ${config.pollSeconds} 秒輪詢。`);
  updateNavigation();
  updateStats();
  window.setInterval(updateStats, 1000);
  receive();
})();
