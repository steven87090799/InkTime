/* Shared visible timestamps, including asynchronously inserted UI content. */
(() => {
  'use strict';
  const pattern = String.raw`\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?`;
  const exact = new RegExp(`^(${pattern})$`);
  const embedded = new RegExp(`\\b${pattern}(?![\\d:])`, 'g');
  const excluded = 'script,style,pre,code,input,textarea,select,option,[contenteditable]:not([contenteditable="false"]),[data-preserve-timestamps]';
  let timeZone = document.documentElement.dataset.displayTimezone || 'Asia/Taipei';
  let formatter;
  const options = {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'};
  try { formatter = new Intl.DateTimeFormat('en-CA', {...options, timeZone}); }
  catch (_) { timeZone = 'Asia/Taipei'; formatter = new Intl.DateTimeFormat('en-CA', {...options, timeZone}); }
  function info(raw, {naiveIsUTC = true} = {}) {
    const value = String(raw ?? '').trim();
    if (!value) return {display:'—', raw:'', valid:false};
    if (!exact.test(value)) return {display:value, raw:value, valid:false};
    const calendar = value.slice(0, 10), clock = value.slice(11, 19);
    const wall = new Date(`${calendar}T${clock}Z`);
    if (Number.isNaN(wall.getTime()) || wall.toISOString().slice(0, 19) !== `${calendar}T${clock}`)
      return {display:value, raw:value, valid:false};
    const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value);
    if (!hasOffset && !naiveIsUTC)
      return {display:`${calendar.replaceAll('-', '/')} ${clock}`, raw:value, valid:true};
    const normalized = value.replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1').replace(/([+-]\d{2})(\d{2})$/, '$1:$2') + (hasOffset ? '' : 'Z');
    const parsed = new Date(normalized);
    if (Number.isNaN(parsed.getTime())) return {display:value, raw:value, valid:false};
    const p = Object.fromEntries(formatter.formatToParts(parsed).map(part => [part.type, part.value]));
    return {display:`${p.year}/${p.month}/${p.day} ${p.hour}:${p.minute}:${p.second}`, raw:value, valid:true};
  }
  window.inktimeDateTimeInfo = info;
  window.inktimeFormatDateTime = (raw, options) => info(raw, options).display;
  function formatText(node) {
    if (!node.parentElement || node.parentElement.closest(excluded)) return;
    const formatted = node.data.replace(embedded, raw => info(raw).display);
    if (formatted !== node.data) node.data = formatted;
  }
  function formatWithin(root) {
    if (root.nodeType === Node.TEXT_NODE) { formatText(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE || root.closest(excluded)) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) formatText(node);
  }
  const observer = new MutationObserver(records => {
    // Only visit changed nodes, never rescan the page for each polling update.
    // Disconnect during our own text edits so they do not enqueue more work.
    observer.disconnect();
    try {
      for (const record of records) {
        if (record.type === 'characterData') formatText(record.target);
        else for (const node of record.addedNodes) formatWithin(node);
      }
    } finally { observe(); }
  });
  function observe() { observer.observe(document.body, {subtree:true,childList:true,characterData:true}); }
  function start() { formatWithin(document.body); observe(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
  else start();
  window.addEventListener('pagehide', () => observer.disconnect());
  window.addEventListener('pageshow', event => { if (event.persisted) start(); });
})();
