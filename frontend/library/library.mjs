// library/library.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { createStore } from "../core/store.mjs";
import { $, api, confirmBox, esc, human, icon, jpost, toast } from "../core/util.mjs";
import { closeMenus, showView, view } from "../ui/views.mjs";
import { engineState, poll } from "../ui/engine.mjs";
import { listState } from "../chat/list.mjs";
import { renderThread } from "../chat/thread.mjs";
import { syncThink } from "../chat/composer.mjs";
import { agRenderModels } from "../modules.mjs";

// ---------------- library
let lib = null, libTab = "chat"; const libraryState = createStore({ runningName: null, runningMode: null });
const activeChat = () => lib?.chat.find((m) => m.active);
const activeImage = () => lib?.image.find((m) => m.active);
function updateHeader() {
  if (view === "chat") {
    const a = activeChat();
    $("hdrTitle").textContent = libraryState.runningName || a?.name || "Local Studio";
    $("hdrSub").textContent = libraryState.runningMode === "vision" ? "Vision" : libraryState.runningMode === "gguf" ? "llama.cpp" : (a?.accel || "");
  } else if (view === "code") { $("hdrTitle").textContent = "Code"; $("hdrSub").textContent = libraryState.runningName || activeChat()?.name || ""; }
  else if (view === "images") { $("hdrTitle").textContent = activeImage()?.name || "Images"; $("hdrSub").textContent = "mflux"; }
  else if (view === "library") { $("hdrTitle").textContent = "Model library"; $("hdrSub").textContent = ""; }
  else if (view === "search") { $("hdrTitle").textContent = "Search"; $("hdrSub").textContent = "EmbeddingGemma 2"; }
  else if (view === "agent") { $("hdrTitle").textContent = "Agent"; $("hdrSub").textContent = ""; }
  else if (view === "voice") { $("hdrTitle").textContent = "Voice"; $("hdrSub").textContent = "On this Mac"; }
  else { $("hdrTitle").textContent = "Settings"; $("hdrSub").textContent = ""; }
}
async function loadLibrary() {
  try { lib = await api("/api/models"); } catch { return; }
  renderMenu(); renderImgModels(); updateHeader(); syncThink(); agRenderModels();
  if (view === "library") renderLibrary();
  const noModel = !lib.chat.some((m) => m.installed);
  if (!listState.current?.messages?.length && view === "chat" && (noModel || loadLibrary._noModel)) renderThread();   // onboarding only
  loadLibrary._noModel = noModel;
  clearTimeout(loadLibrary._t);
  if ([...lib.chat, ...lib.image].some((m) => m.downloading)) loadLibrary._t = setTimeout(loadLibrary, 3000);
}
function tagsHTML(m) {
  const t = [];
  if (m.accel) t.push(`<span class="tag ${m.backend === "gguf" ? "gguf" : "accel"}">${esc(m.accel)}</span>`);
  if (m.vision) t.push(`<span class="tag vision">Vision</span>`);
  if (m.custom) t.push(`<span class="tag">Custom</span>`);
  return t.join("");
}
const dlText = (m) => `Downloading · ${human(m.size_bytes)} of ~${m.size_gb} GB`;
const cardSig = (m) => [m.installed, m.downloading, m.active, m.download_error].map(Number).join("");
// Re-render a card list only when a card's state changes; otherwise just move the progress bars,
// so buttons under the pointer (Cancel, Use) aren't replaced mid-click during downloads.
function patchCards(el, html, models) {
  const cards = el.querySelectorAll("[data-card]");
  const same = cards.length === models.length && models.every((m, i) => cards[i].dataset.card === m.id && cards[i].dataset.sig === cardSig(m));
  if (!same) { el.innerHTML = html; return; }
  models.forEach((m, i) => {
    if (!m.downloading) return;
    const bar = cards[i].querySelector("[data-p]"), txt = cards[i].querySelector("[data-pt]");
    if (bar) bar.style.width = Math.round((m.progress || 0) * 100) + "%";
    if (txt) txt.textContent = dlText(m);
  });
}
function cardHTML(m) {
  const inst = m.installed, dl = m.downloading;
  const size = inst ? human(m.size_bytes) : `${m.size_gb} GB`;
  let acts;
  if (dl) acts = `<div style="flex:1"><div class="dlbar"><i data-p style="width:${Math.round((m.progress || 0) * 100)}%"></i></div><div class="sub" data-pt style="margin-top:5px">${dlText(m)}</div></div><button class="btn sm" data-cancel="${m.id}">Cancel</button>`;
  else if (!inst) acts = `<button class="btn primary sm" data-dl="${m.id}">${icon("i-dl")}Download · ${m.size_gb} GB</button>${m.download_error ? `<span class="sub" style="color:var(--bad)">Download failed, try again</span>` : ""}<span class="grow"></span>${m.custom ? `<button class="btn sm danger" data-del="${m.id}">Remove</button>` : ""}`;
  else acts = `${m.active ? `<span class="badge ok">${icon("i-check", "width:13px;height:13px")}Active</span>` : `<button class="btn primary sm" data-use="${m.id}" data-kind="${m.kind}">Use</button>`}<span class="grow"></span><button class="btn sm danger" data-del="${m.id}">${icon("i-trash")}Delete</button>`;
  const facts = [`<span>Size <b>${size}</b></span>`];
  if (m.ram_gb) facts.push(`<span>Memory <b>~${m.ram_gb} GB</b></span>`);
  if (m.steps) facts.push(`<span>Default <b>${m.steps} steps</b></span>`);
  return `<div class="mcard${m.active ? " active" : ""}" data-card="${m.id}" data-sig="${cardSig(m)}"><div class="top"><div style="min-width:0"><h4>${esc(m.name)}</h4><div class="repo" title="${esc(m.repo)}">${esc(m.repo)}${m.file ? " · " + esc(m.file.replace(/\.gguf$/, "")) : ""}</div></div><div class="tags">${tagsHTML(m)}</div></div>
    <p>${esc(m.blurb || "")}</p><div class="facts">${facts.join("")}</div><div class="acts">${acts}</div></div>`;
}
function renderLibrary() {
  $("libDisk").textContent = `${human(lib.disk_free_bytes)} free.`;
  ["chat", "image", "search"].forEach((t) => { $("lib" + t[0].toUpperCase() + t.slice(1)).style.display = t === libTab ? "" : "none"; });
  $("libTabs").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.t === libTab));
  $("libFilterWrap").style.display = libTab === "chat" ? "" : "none";
  const f = $("libFilter").value;
  const chat = lib.chat.filter((m) => f === "all" || (f === "installed" ? m.installed : f === "gguf" ? m.backend === "gguf" : m.backend !== "gguf"));
  let html = chat.map(cardHTML).join("");
  if (!lib.llama_cpp && chat.some((m) => m.backend === "gguf")) html = `<div class="empty" style="grid-column:1/-1">llama.cpp isn't installed, so GGUF models can't run yet. Use Settings → Update engines to install it.</div>` + html;
  if (!chat.length) $("libChat").innerHTML = `<div class="empty">No models match this filter.</div>`;
  else if (!lib.llama_cpp && chat.some((m) => m.backend === "gguf")) $("libChat").innerHTML = html;
  else patchCards($("libChat"), html, chat);
  patchCards($("libImage"), lib.image.map(cardHTML).join(""), lib.image);
  $("libOtherWrap").style.display = lib.other.length && libTab !== "search" ? "" : "none";
  $("libOther").innerHTML = lib.other.map((o) => `<div class="row"><div class="grow"><div class="ttl">${esc(o.repo)}</div></div><div class="size">${human(o.size_bytes)}</div><button class="btn sm danger" data-delrepo="${esc(o.repo)}">Delete</button></div>`).join("");
}
$("libTabs").onclick = (e) => { const b = e.target.closest("button"); if (b) { libTab = b.dataset.t; renderLibrary(); if (libTab === "search") $("hfQ").focus(); } };
$("libFilter").onchange = renderLibrary;
document.querySelector("#v-library .page").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const find = (id) => [...lib.chat, ...lib.image].find((m) => m.id === id);
  try {
    if (b.dataset.dl) { await jpost("/api/models/download", { id: b.dataset.dl }); toast(`Downloading ${find(b.dataset.dl).name}…`); }
    else if (b.dataset.cancel) { await jpost("/api/models/cancel", { id: b.dataset.cancel }); toast("Download cancelled"); }
    else if (b.dataset.use) { await setActive(b.dataset.use, b.dataset.kind); }
    else if (b.dataset.del) {
      const m = find(b.dataset.del);
      if (!(await confirmBox(`Delete ${m.name}?`, m.installed ? `Frees about ${human(m.size_bytes)}. You can download it again any time.` : "Removes it from your library.", "Delete"))) return;
      const r = await api(`/api/models?id=${encodeURIComponent(m.id)}`, { method: "DELETE" }); toast(`Deleted · freed ${human(r.freed_bytes)}`);
    } else if (b.dataset.delrepo) {
      if (!(await confirmBox(`Delete ${b.dataset.delrepo}?`, "Removes these cached files from disk.", "Delete"))) return;
      const r = await api(`/api/models?repo=${encodeURIComponent(b.dataset.delrepo)}`, { method: "DELETE" }); toast(`Freed ${human(r.freed_bytes)}`);
    } else return;
  } catch (err) { toast(err.message); }
  loadLibrary();
});
async function setActive(id, kind = "chat") {
  await jpost("/api/models/active", { id, kind });
  const m = [...lib.chat, ...lib.image].find((x) => x.id === id);
  toast(kind === "image" ? `Images now use ${m.name}` : engineState === "down" ? `${m.name} selected` : `Switching to ${m.name}…`);
  await loadLibrary(); setTimeout(poll, 600);
}
function renderMenu() {
  const inst = lib.chat.filter((m) => m.installed);
  $("mmList").innerHTML = inst.map((m) => `<div class="mmrow${m.active ? " on" : ""}" data-mm="${m.id}">${icon("i-check", "").replace('class="i"', 'class="i ck"')}<div class="nm">${esc(m.name)}<div class="d">${esc(m.accel || "")}${m.vision ? " · Vision" : ""} · ~${m.ram_gb || m.size_gb} GB</div></div></div>`).join("") || `<div class="mmrow"><div class="nm d">No chat models downloaded yet</div></div>`;
}
$("mmList").onclick = async (e) => { const r = e.target.closest("[data-mm]"); if (!r) return; closeMenus(); if (listState.streaming) return toast("Wait for the reply to finish"); try { await setActive(r.dataset.mm); } catch (err) { toast(err.message); } };
function renderImgModels() {
  const sel = $("imgModel"), cur = sel.value;
  const inst = lib.image.filter((m) => m.installed);
  sel.innerHTML = inst.map((m) => `<option value="${m.id}"${m.active ? " selected" : ""}>${esc(m.name)}</option>`).join("") + `<option value="__lib">Get more models…</option>`;
  if (cur && inst.some((m) => m.id === cur)) sel.value = cur;
  const m = lib.image.find((x) => x.id === sel.value) || activeImage();
  if (m) { $("imgLead").textContent = `${m.name} · ${m.blurb}`; setStepOptions(m.steps); }
}
function setStepOptions(def) {
  const opts = [...new Set([def, 4, 8, 20, 30, 40].filter(Boolean))].sort((a, b) => a - b);
  $("imgSteps").innerHTML = opts.map((n) => `<option value="${n}"${n === def ? " selected" : ""}>${n} steps${n === def ? " · recommended" : ""}</option>`).join("");
}
$("imgModel").onchange = async () => {
  if ($("imgModel").value === "__lib") { libTab = "image"; showView("library"); return; }
  try { await setActive($("imgModel").value, "image"); } catch (err) { toast(err.message); }
};
// Hugging Face search
let hfKind = "mlx";
$("hfKind").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; hfKind = b.dataset.v; $("hfKind").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); if ($("hfQ").value.trim()) hfSearch(); };
$("hfGo").onclick = () => hfSearch();
$("hfQ").addEventListener("keydown", (e) => { if (e.key === "Enter") hfSearch(); });
async function hfSearch() {
  const q = $("hfQ").value.trim(); if (!q) return;
  $("hfResults").innerHTML = `<div class="sub">Searching…</div>`;
  let rows; try { rows = await api(`/api/hf/search?q=${encodeURIComponent(q)}&kind=${hfKind}`); } catch (e) { $("hfResults").innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  $("hfResults").innerHTML = rows.length ? rows.map((r) => `<div class="hfrow" data-repo="${esc(r.repo)}"><div class="grow"><div class="ttl">${esc(r.repo)}</div><div class="sub">${r.downloads.toLocaleString()} downloads · ${r.likes} likes${r.pipeline ? " · " + esc(r.pipeline) : ""}</div></div><span class="hfact"><button class="btn sm" data-inspect>Choose…</button></span></div>`).join("") : `<div class="empty">No ${hfKind.toUpperCase()} models found for “${esc(q)}”.</div>`;
}
$("hfResults").onclick = async (e) => {
  const row = e.target.closest(".hfrow"); if (!row) return;
  const repo = row.dataset.repo, act = row.querySelector(".hfact");
  if (e.target.closest("[data-inspect]")) {
    act.innerHTML = `<span class="sub">Checking files…</span>`;
    let info; try { info = await api(`/api/hf/files?repo=${encodeURIComponent(repo)}`); } catch (err) { act.innerHTML = `<span class="err">${esc(err.message)}</span>`; return; }
    if (hfKind === "gguf") {
      if (!info.ggufs.length) { act.innerHTML = `<span class="sub">No GGUF files</span>`; return; }
      const pick = info.ggufs.find((g) => /Q4_K_M/i.test(g.file)) || info.ggufs[0];
      act.innerHTML = `<select data-file>${info.ggufs.map((g) => `<option value="${esc(g.file)}"${g === pick ? " selected" : ""}>${esc(g.file.split("/").pop())} · ${human(g.size)}</option>`).join("")}</select>
        ${info.mmproj.length ? `<label class="sub" style="display:inline-flex;gap:5px;align-items:center"><input type="checkbox" data-mmproj="${esc((info.mmproj.find((m) => /F16/i.test(m.file)) || info.mmproj[0]).file)}" checked>Vision</label>` : ""}
        <button class="btn primary sm" data-add>Download</button>`;
    } else {
      act.innerHTML = `<span class="sub">${human(info.total)}</span> ${info.has_config ? `<button class="btn primary sm" data-add>Download</button>` : `<span class="sub">Not an MLX model</span>`}`;
    }
    return;
  }
  if (e.target.closest("[data-add]")) {
    const body = { repo, backend: hfKind };
    if (hfKind === "gguf") { body.file = act.querySelector("[data-file]").value; const mm = act.querySelector("[data-mmproj]"); if (mm?.checked) body.mmproj = mm.dataset.mmproj; }
    try { const m = await jpost("/api/models/custom", body); toast(`Downloading ${m.name}…`); act.innerHTML = `<span class="badge warn">Downloading — see Chat models</span>`; loadLibrary(); }
    catch (err) { toast(err.message); }
  }
};

export {
  activeChat, activeImage, cardHTML, cardSig, dlText, hfKind, hfSearch, lib, libraryState, libTab, loadLibrary,
  patchCards, renderImgModels, renderLibrary, renderMenu, setActive, setStepOptions, tagsHTML, updateHeader
};
