const key = "gooddisplay_spectra6";

export async function applySpectra6Preset() {
  const preview = await window.inktimeFetch(`/api/v1/settings/presets/${key}/preview`, { method: "POST" });
  const detail = await window.inktimeDecodeJson(preview);
  if (!preview.ok) throw new Error(detail.message || "無法預覽 Preset");
  const message = `將修改：${detail.changed_keys.join("、") || "無"}\n相容裝置：${detail.affected_devices.length} 台；不相容裝置：${detail.incompatible_devices.length} 台。\n既有 Release 不會修改，下一次渲染才生效。`;
  if (!window.confirm(message)) return;
  const result = await window.inktimeFetch(`/api/v1/settings/presets/${key}/apply`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
  const payload = await window.inktimeDecodeJson(result);
  if (!result.ok) throw new Error(payload.message || "套用失敗");
  window.alert("已成功套用微雪 Spectra 6 原廠設定！\n新設定將於下一次渲染生效。");
  window.location.reload();
}

for (const button of document.querySelectorAll("[data-spectra6-preset]")) {
  button.addEventListener("click", () => applySpectra6Preset().catch((error) => window.alert(error.message)));
}
