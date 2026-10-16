window.inktimeBindCropEditor = ({photoSelect, x, y, saveButton, autoButton, status, refresh}) => {
  if (!saveButton || !autoButton) return;
  let pending = false;
  const persist = async mode => {
    if (pending || !photoSelect.value) return;
    const photoId = photoSelect.value, requestedX = x.value, requestedY = y.value;
    const option = [...photoSelect.options].find(value => value.value === photoId);
    const payload = mode === 'manual' ? {mode, x: Number(requestedX) / 100, y: Number(requestedY) / 100} : {mode};
    pending = true; saveButton.disabled = true; autoButton.disabled = true;
    try {
      const response = await window.inktimeFetch(`/api/v1/photos/${encodeURIComponent(photoId)}/crop`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
      });
      const data = await window.inktimeDecodeJson(response);
      if (!response.ok) throw Error(data.message || '裁切儲存失敗');
      const crop = data.crop || {photo_id: photoId, focus_x: mode === 'manual' ? payload.x : Number(option.dataset.autoX ?? 0.5),
        focus_y: mode === 'manual' ? payload.y : Number(option.dataset.autoY ?? 0.5), manual: mode === 'manual'};
      if (crop.photo_id !== photoId || !Number.isFinite(crop.focus_x) || !Number.isFinite(crop.focus_y)) throw Error('裁切回覆身分或焦點不正確');
      option.dataset.focusX = crop.focus_x; option.dataset.focusY = crop.focus_y;
      option.dataset.manualCrop = crop.manual ? '1' : '0';
      if (Number.isFinite(crop.auto_x)) option.dataset.autoX = crop.auto_x;
      if (Number.isFinite(crop.auto_y)) option.dataset.autoY = crop.auto_y;
      if (photoSelect.value === photoId) {
        // Keep slider edits made after the request began; update only saved metadata.
        refresh(x.value === requestedX && y.value === requestedY);
      }
      window.inktimeSetStatus(status, `${mode === 'manual' ? '已儲存裁切焦點' : '已恢復自動裁切'}：${photoId}`, 'success');
    } catch (error) {
      window.inktimeSetStatus(status, `${error.message || '連線失敗'}；請查核此照片的儲存結果。`, 'error');
    } finally {
      pending = false; saveButton.disabled = false; autoButton.disabled = false;
    }
  };
  saveButton.onclick = () => persist('manual');
  autoButton.onclick = () => persist('auto');
};
