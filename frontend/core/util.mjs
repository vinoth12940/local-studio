// core/util.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).

const $ = (id) => document.getElementById(id);
const DEFAULT_MAX_TOKENS = 16384;   // thinking + answer; the server trims it to fit the context
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const gib = (b) => (b / 2 ** 30).toFixed(1);
const human = (b) => b >= 2 ** 30 ? `${(b / 2 ** 30).toFixed(1)} GB` : b >= 2 ** 20 ? `${(b / 2 ** 20).toFixed(1)} MB` : b >= 1024 ? `${(b / 1024).toFixed(0)} KB` : `${b} B`;
const store = { get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} } };
const icon = (id, st = "") => `<svg class="i" style="${st}"><use href="#${id}"/></svg>`;
const newId = () => [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, "0")).join("");
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let j = null; try { j = await r.json(); } catch {}
  if (!r.ok) { const e = new Error((j && (j.detail || j.error)) || `HTTP ${r.status}`); e.code = j?.code; e.data = j; throw e; }
  return j;
}
const jpost = (path, body, method = "POST") => api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 2600); }
function confirmBox(title, body, yes = "Delete") {
  return new Promise((res) => {
    $("cTitle").textContent = title; $("cBody").textContent = body; $("cYes").textContent = yes;
    const d = $("confirmDlg"); let answered = false;
    const done = (v) => { if (answered) return; answered = true; d.close(); res(v); };
    $("cYes").onclick = () => done(true); $("cNo").onclick = () => done(false); d.onclose = () => done(false);
    d.showModal();
  });
}

// Assigning innerHTML rebuilds every <option>, which closes an open dropdown and drops its keyboard focus. A periodic refresh must
// therefore leave the list alone unless its markup changed, and touch the selection only when it differs.
function setOptions(el, html, value) {
  if (el._optSig !== html) { el.innerHTML = html; el._optSig = html; }
  if (value !== undefined && el.value !== value) el.value = value;
}

export {
  $, api, confirmBox, DEFAULT_MAX_TOKENS, esc, gib, human, icon, jpost, newId, setOptions, store, toast
};
