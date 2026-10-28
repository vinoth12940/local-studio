// chat/attachments.mjs: moved from index.html's inline script in the refactor's P2 split.
import { createStore } from "../core/store.mjs";
import { $, api, esc, icon, toast } from "../core/util.mjs";
import { updateSend } from "../modules.mjs";

// attachments
const attachmentsState = createStore({ pending: [] });            // {key, name, busy, meta?, error?, preview?}
const DOC_ICON = (n) => /\.(xlsx|xlsm|csv|tsv)$/i.test(n) ? "i-sheet" : "i-file";
function attMeta(m) {
  if (m.kind === "image") return m.scanned ? `Scanned PDF · ${m.images.length} of ${m.pages} pages` : `Image · ${m.width}×${m.height}`;
  const bits = [];
  if (m.pages) bits.push(`${m.pages} page${m.pages > 1 ? "s" : ""}`);
  if (m.sheets) bits.push(`${m.sheets} sheet${m.sheets > 1 ? "s" : ""}`);
  const t = m.tokens_est || 0;
  bits.push(t < 1000 ? `~${Math.max(1, Math.round(t))} tokens` : `~${(t / 1000).toFixed(1)}k tokens`);
  return bits.join(" · ");
}
function attThumb(m) { return m.images?.length ? `/api/attachments/${m.id}/${m.images[0]}` : null; }
function renderPending() {
  $("atts").innerHTML = attachmentsState.pending.map((p, i) => {
    const m = p.meta, isImg = (m && m.kind === "image" && !m.scanned) || (!m && p.preview);
    const thumb = m ? attThumb(m) : p.preview;
    const ic = thumb ? `<img src="${thumb}" alt="">` : icon(DOC_ICON(p.name));
    const sub = p.error ? `<div class="mt warn">${esc(p.error)}</div>` : p.busy ? `<div class="mt">Reading…</div>` : `<div class="mt${m.truncated ? " warn" : ""}">${esc(attMeta(m))}${m.truncated ? " · truncated" : ""}</div>`;
    return `<div class="att${isImg ? " img" : ""}${p.busy ? " busy" : ""}" title="${esc(p.name)}"><div class="ic">${ic}</div>${isImg ? "" : `<div style="min-width:0"><div class="nm">${esc(p.name)}</div>${sub}</div>`}<button class="x" data-rm="${i}" title="Remove">${icon("i-x")}</button></div>`;
  }).join("");
  updateSend();
}
$("atts").onclick = (e) => { const b = e.target.closest("[data-rm]"); if (b) { attachmentsState.pending.splice(+b.dataset.rm, 1); renderPending(); } };
async function addFiles(files) {
  for (const f of files) {
    const p = { key: Math.random(), name: f.name || "pasted-image.png", busy: true, preview: f.type.startsWith("image/") ? URL.createObjectURL(f) : null };
    attachmentsState.pending.push(p); renderPending();
    const fd = new FormData(); fd.append("file", f, p.name);
    try { p.meta = await api("/api/attachments", { method: "POST", body: fd }); }
    catch (err) { p.error = err.message; toast(`${p.name}: ${err.message}`); }
    p.busy = false; renderPending();
  }
}
$("attachBtn").onclick = () => $("fileInput").click();
$("fileInput").onchange = (e) => { addFiles([...e.target.files]); e.target.value = ""; };
let dragDepth = 0;
const chatView = $("v-chat");
chatView.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); dragDepth++; $("dropzone").classList.add("on"); } });
chatView.addEventListener("dragover", (e) => { if (e.dataTransfer?.types?.includes("Files")) e.preventDefault(); });
chatView.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("dropzone").classList.remove("on"); } });
chatView.addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; $("dropzone").classList.remove("on"); if (e.dataTransfer.files.length) addFiles([...e.dataTransfer.files]); });
$("input").addEventListener("paste", (e) => { const files = [...(e.clipboardData?.files || [])]; if (files.length) { e.preventDefault(); addFiles(files); } });

export {
  addFiles, attachmentsState, attMeta, attThumb, chatView, DOC_ICON, dragDepth, renderPending
};
