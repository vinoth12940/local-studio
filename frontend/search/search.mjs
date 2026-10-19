// search/search.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { $, api, confirmBox, esc, human, icon, jpost, store, toast, view } from "../modules.mjs";

// ---------------- search
let sr = null, srTimer = 0, srSeq = 0, useFiles = store.get("useFiles", false); const searchState = { noFilesOnce: false };
function syncFiles() {
  const can = !!(sr && sr.enabled && sr.running && !sr.error);
  $("filesPill").classList.toggle("on", useFiles && can);
  $("filesPill").disabled = !can;
  $("filesPill").title = can ? "Answer from your indexed files" : "Turn on Search in Settings to use your files";
  store.set("useFiles", useFiles);
}
$("filesPill").onclick = () => { useFiles = !useFiles; syncFiles(); };
const srHome = (p) => p.replace(/^\/Users\/[^/]+/, "~");
const srLoc = (l) => !l ? "" : /^p\d+$/.test(l) ? `page ${l.slice(1)}` : /^\d+s$/.test(l) ? `${Math.floor(+l.slice(0, -1) / 60)}:${String(+l.slice(0, -1) % 60).padStart(2, "0")}` : l;
const SR_ICON = { document: "i-file", image: "i-image", audio: "i-spark", video: "i-image", name: "i-file" };
const srSecs = (l) => /^\d+s$/.test(l || "") ? +l.slice(0, -1) : 0;
const SR_PAUSED = { user: "Paused", busy: "Waiting while chat or image generation is running", battery: "Paused on battery power" };
const SR_OFF = "Turn it on to index your documents, images and audio. Nothing is read or downloaded until you do.";
function srStatusLine(s, buttons = true) {
  const bad = (t) => `<span style="color:var(--bad)">${esc(t)}</span>`;
  if (s.setup?.status === "running") return esc(s.setup.step || "Setting up");
  if (!s.running) return s.setup?.status === "error" ? bad(s.setup.error) : "";
  if (s.error) return bad(s.error);
  if (s.problem && s.state !== "indexing") return bad(s.problem);
  if (s.state === "starting") return "Starting";
  const done = (s.files_done + s.files_error).toLocaleString(), total = s.files_total.toLocaleString();
  const btn = (v, label) => buttons ? ` <button class="btn sm" data-srpause="${v}">${label}</button>` : "";
  if (s.state === "paused") return `${SR_PAUSED[s.paused_reason] || "Paused"} · ${done} of ${total} files` + (s.paused_reason === "user" ? btn(0, "Resume") : "");
  if (s.state === "indexing") return `Indexing ${done} of ${total} files${s.current ? " · " + esc(s.current.split("/").pop()) : ""}` + btn(1, "Pause");
  return `${s.files_done.toLocaleString()} files indexed · up to date`;
}
async function loadSearch() {
  try { sr = await api("/api/search/status"); } catch { return; }
  const on = sr.enabled && sr.running, busy = sr.setup?.status === "running", starting = sr.enabled && !sr.running && !busy;
  $("srOn").style.display = on ? "" : "none"; $("srOff").style.display = on ? "none" : "";
  $("srEnable").disabled = busy; $("srEnable").textContent = busy ? "Setting up" : "Turn on Search";
  $("srOffTtl").textContent = starting ? "Search is starting" : "Search is off";
  $("srOffMsg").innerHTML = srStatusLine(sr) || (starting ? "This takes up to a minute after the app opens. If it stays here, turn it on again." : SR_OFF);
  $("srStatus").innerHTML = srStatusLine(sr);
  $("srSw").checked = sr.enabled || busy; $("srSw").disabled = busy;
  $("srSetSum").innerHTML = (sr.enabled || busy ? srStatusLine(sr, false) : "") || `Find your files by what they contain. Uses about ${Math.ceil(sr.model.size_gb + 1)} GB for the model and search components.`;
  $("srSetBody").style.display = on ? "" : "none";
  $("srDelRow").style.display = !sr.enabled && !busy && sr.index_on_disk ? "" : "none";
  syncFiles();
  if (!on) return;
  $("srFolders").innerHTML = sr.roots.map((p) => `<div class="row"><div class="grow"><div class="ttl">${esc(srHome(p))}</div>${sr.no_access.includes(p) ? `<div class="sub" style="color:var(--bad)">No access. Allow it in System Settings, Privacy &amp; Security, Files and Folders.</div>` : ""}</div><button class="btn sm" data-srrm="${esc(p)}">Remove</button></div>`).join("");
  $("srBattery").checked = sr.on_battery;
  $("srIdxSub").textContent = `${sr.files_done.toLocaleString()} files · ${human(sr.index_bytes)} on disk${sr.files_error ? ` · ${sr.files_error.toLocaleString()} could not be read` : ""}`;
  $("srErrBtn").style.display = sr.files_error ? "" : "none";
}
async function srSave(patch) { try { await jpost("/api/search/settings", patch, "PUT"); } catch (e) { toast(e.message); } loadSearch(); }
function srAddFolder(path) {
  path = (path || "").trim().replace(/\/+$/, ""); if (!path || !sr) return;
  if (sr.removed.includes(path)) return srSave({ removed: sr.removed.filter((p) => p !== path) });
  if (sr.roots.includes(path)) return toast("That folder is already indexed");
  srSave({ folders: [...sr.folders, path] });
}
function srRemoveFolder(path) {
  if (sr.folders.includes(path)) return srSave({ folders: sr.folders.filter((p) => p !== path) });
  srSave({ removed: [...sr.removed, path] });
}
async function srEnable() { try { await jpost("/api/search/enable"); } catch (e) { toast(e.message); } loadSearch(); }
$("srEnable").onclick = srEnable;
$("srSw").onchange = async () => {
  if ($("srSw").checked) return srEnable();
  try { await jpost("/api/search/disable"); } catch (e) { toast(e.message); }
  loadSearch();
};
$("srFolders").onclick = (e) => { const b = e.target.closest("[data-srrm]"); if (b) srRemoveFolder(b.dataset.srrm); };
$("srAddPath").onclick = () => { srAddFolder($("srPath").value); $("srPath").value = ""; };
$("srPath").addEventListener("keydown", (e) => { if (e.key === "Enter") $("srAddPath").click(); });
$("srChoose").onclick = async () => { try { const r = await jpost("/api/search/choose-folder"); if (r.path) srAddFolder(r.path); } catch (e) { toast(e.message); } };
$("srBattery").onchange = () => srSave({ on_battery: $("srBattery").checked });
$("srRebuild").onclick = async () => {
  if (!await confirmBox("Rebuild the search index?", "Every file is read and indexed again. This can take an hour or more.", "Rebuild")) return;
  try { await jpost("/api/search/rebuild"); toast("Rebuilding the index"); } catch (e) { toast(e.message); }
  loadSearch();
};
$("srDelete").onclick = async () => {
  if (!await confirmBox("Delete the search index?", "Your files are not touched. Turning Search on again rebuilds it.")) return;
  try { await api("/api/search/index", { method: "DELETE" }); } catch (e) { toast(e.message); }
  loadSearch();
};
$("srErrBtn").onclick = async () => {
  const box = $("srErrList"); if (box.style.display === "") { box.style.display = "none"; return; }
  try {
    const { errors } = await api("/api/search/errors");
    box.innerHTML = errors.map((x) => `<div class="row"><div class="grow"><div class="ttl">${esc(srHome(x.path))}</div><div class="sub">${esc(x.error)}</div></div></div>`).join("");
    box.style.display = "";
  } catch (e) { toast(e.message); }
};
document.addEventListener("click", (e) => { const b = e.target.closest("[data-srpause]"); if (b) srSave({ paused: b.dataset.srpause === "1" }); });
function srRow(r, i) {
  const dir = srHome(r.path.slice(0, r.path.length - r.name.length - 1)), loc = srLoc(r.locator);
  const th = r.kind === "image" ? `<img src="/api/search/thumb/${+r.file_id}" alt="" loading="lazy">`
    : r.kind === "video" ? `<img src="/api/search/thumb/${+r.file_id}?t=${srSecs(r.locator)}" alt="" loading="lazy">`   // the frame at the matching moment
    : icon(SR_ICON[r.kind] || "i-file");
  const open = `data-open="${r.file_id}" data-path="${esc(r.path)}"`;
  return `<div class="srrow" data-i="${i}"><div class="th">${th}</div><div style="flex:1; min-width:0"><div class="nm">${esc(r.name)}</div><div class="pt">${esc(dir)}${loc ? " · " + esc(loc) : ""}</div>${r.snippet ? `<div class="sn">${esc(r.snippet)}</div>` : ""}</div>
    <div class="acts"><button class="btn sm" ${open}>Open</button><button class="btn sm" ${open} data-reveal="1">Reveal</button></div></div>`;
}
async function runSearch() {
  const q = $("srQuery").value.trim(), seq = ++srSeq;                 // srSeq: a slow earlier query must not overwrite a newer one
  if (!q) { srRes = []; srClosePreview(); $("srResults").innerHTML = ""; return; }
  let res;
  try { res = (await jpost("/api/search", { query: q, kind: $("srKind").value })).results; }
  catch (e) { if (seq === srSeq) $("srResults").innerHTML = `<p class="lead" style="margin-top:18px">${esc(e.message)}</p>`; return; }
  if (seq !== srSeq) return;
  srRes = res;
  $("srResults").innerHTML = res.length ? res.map(srRow).join("") : `<p class="lead" style="margin-top:18px">Nothing found.</p>`;
  // Show the best match straight away, as the user types. Not on a narrow window, where the preview
  // covers the search box, and not after the user has closed it.
  if (res.length && srAuto && innerWidth > 980) srSelect(0); else srClosePreview();
}
function srOpen(b) { return jpost("/api/search/open", { file_id: +b.dataset.open, path: b.dataset.path, reveal: b.dataset.reveal === "1" }).catch((e) => toast(e.message)); }
$("srQuery").addEventListener("input", () => { clearTimeout(srTimer); srTimer = setTimeout(runSearch, 300); });
$("srQuery").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { clearTimeout(srTimer); runSearch(); }
  else if (e.key === "ArrowDown" && srRes.length) { e.preventDefault(); srAuto = true; srSelect(Math.min(srRes.length - 1, srSel + 1), true); }
  else if (e.key === "ArrowUp" && srSel > 0) { e.preventDefault(); srSelect(srSel - 1, true); }
  else if (e.key === "Escape" && srSel >= 0) { srAuto = false; srClosePreview(); }
});
$("srKind").onchange = runSearch;
$("srResults").onclick = (e) => {
  const b = e.target.closest("[data-open]"); if (b) return srOpen(b);
  const row = e.target.closest(".srrow"); if (row) { srAuto = true; srSelect(+row.dataset.i); }
};
// ---- preview: the selected result, shown at the place that matched
let srRes = [], srSel = -1, srPvState = null, srAuto = true, srPvSeq = 0;
const SR_STOP = new Set("the and for are was were with that this from have has had you your what when where which who how why did does about into out not but can all any our its".split(" "));
function srMark(text, q) {
  const words = [...new Set(q.toLowerCase().match(/[\p{L}\p{N}]{3,}/gu) || [])].filter((w) => !SR_STOP.has(w)).slice(0, 8);
  const html = esc(text);
  return words.length ? html.replace(new RegExp(`(${words.join("|")})`, "gi"), "<mark>$1</mark>") : html;
}
function srClosePreview() {
  srSel = -1; srPvState = null; srPvSeq++;
  $("srPv").style.display = "none"; $("srPv").innerHTML = "";
  $("srBody").classList.remove("pv"); $("srWide").classList.remove("wide");
  document.querySelectorAll(".srrow.picked").forEach((x) => x.classList.remove("picked"));
}
async function srSelect(i, byKey = false) {
  const r = srRes[i]; if (!r) return;
  const seq = ++srPvSeq;                                     // typing selects result 0 again and again: only the latest may draw
  srSel = i; srPvState = null;
  document.querySelectorAll(".srrow").forEach((x) => x.classList.toggle("picked", +x.dataset.i === i));
  $("srBody").classList.add("pv"); $("srWide").classList.add("wide");
  if (byKey) document.querySelector(`.srrow[data-i="${i}"]`)?.scrollIntoView({ block: "nearest" });   // a click must not move the page
  const dir = srHome(r.path.slice(0, r.path.length - r.name.length - 1)), open = `data-open="${+r.file_id}" data-path="${esc(r.path)}"`;
  const pv = $("srPv"); pv.style.display = "";
  pv.innerHTML = `<div class="ph"><div style="flex:1; min-width:0"><div class="nm">${esc(r.name)}</div><div class="pt">${esc(dir)}</div></div>
    <button class="btn sm" ${open}>Open</button><button class="btn sm" ${open} data-reveal="1">Reveal</button><button class="iconbtn" id="srPvClose" title="Close preview">${icon("i-x")}</button></div>
    <div class="pb" id="srPvBody"><span class="shimmer">Loading preview</span></div>`;
  let info;
  try { info = await api(`/api/search/preview/${+r.file_id}` + (r.chunk_id ? `?chunk_id=${+r.chunk_id}` : "")); }
  catch (e) { if (seq === srPvSeq) $("srPvBody").innerHTML = `<p class="lead">${esc(e.message)}</p>`; return; }
  if (seq !== srPvSeq) return;                               // the user moved on while this was loading
  const body = $("srPvBody"), q = $("srQuery").value;
  srPvState = { info, page: info.page || 1, pages: 1 };
  if (info.type === "page") {
    body.innerHTML = `<div class="pgbar"><button class="btn sm" id="srPgPrev">Previous</button><span id="srPgTxt">Page ${+info.page}</span><button class="btn sm" id="srPgNext">Next</button></div><img class="pg" id="srPgImg" alt="">`;
    srShowPage(info.page);
  } else if (info.type === "text") {
    body.innerHTML = `<div class="pvtext">${info.before ? `<div class="ctx">${srMark(info.before, q)}</div>` : ""}<div class="hit">${srMark(info.match, q)}</div>${info.after ? `<div class="ctx">${srMark(info.after, q)}</div>` : ""}</div>`;
    const hit = body.querySelector(".hit"); body.scrollTop = Math.max(0, hit.offsetTop - body.offsetTop - 60);   // scroll the panel, not the page
  } else if (info.type === "image") body.innerHTML = `<img class="pg" src="/api/search/image/${+info.file_id}" alt="">`;
  else if (info.type === "video") body.innerHTML = `<video controls preload="metadata" playsinline style="width:100%; border-radius:8px; background:#000" src="/api/search/media/${+info.file_id}#t=${+info.start}"></video><p class="lead" style="margin:12px 2px 0">The match is at ${srLoc(info.start + "s")}.</p>`;
  else if (info.type === "audio") body.innerHTML = `<audio controls preload="metadata" style="width:100%" src="/api/search/media/${+info.file_id}#t=${+info.start}"></audio><p class="lead" style="margin:12px 2px 0">The match starts at ${srLoc(info.start + "s")}.</p>`;
  else body.innerHTML = `<p class="lead">This file was found by its name. Its contents are not indexed, so there is nothing to preview.</p>`;
}
async function srShowPage(n) {
  const st = srPvState; if (!st) return;
  const marked = n === st.info.page && st.info.chunk_id;     // only the matching page carries the highlight
  try {
    const res = await fetch(`/api/search/page/${+st.info.file_id}/${n}` + (marked ? `?chunk_id=${+st.info.chunk_id}` : ""));
    if (!res.ok) throw new Error("This page could not be shown.");
    const blob = await res.blob();
    if (srPvState !== st) return;
    st.page = n; st.pages = +res.headers.get("X-Pages") || 1;
    const img = $("srPgImg"), old = img.src;
    img.src = URL.createObjectURL(blob);
    if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    $("srPgTxt").textContent = `Page ${n} of ${st.pages}`; $("srPgPrev").disabled = n <= 1; $("srPgNext").disabled = n >= st.pages;
  } catch (e) { toast(e.message); }
}
$("srPv").onclick = (e) => {
  if (e.target.closest("#srPvClose")) { srAuto = false; return srClosePreview(); }   // closed on purpose: stay closed until a result is picked
  if (e.target.closest("#srPgPrev")) return srShowPage(srPvState.page - 1);
  if (e.target.closest("#srPgNext")) return srShowPage(srPvState.page + 1);
  const b = e.target.closest("[data-open]"); if (b) srOpen(b);
};
// Also while the service is still coming up after launch, so the My files pill enables without a visit to Search.
setInterval(() => { if (view === "search" || view === "manage" || sr?.setup?.status === "running" || (sr?.enabled && !sr.running)) loadSearch(); }, 3000);

export {
  loadSearch, runSearch, searchState, sr, SR_ICON, SR_OFF, SR_PAUSED, SR_STOP, srAddFolder, srAuto, srClosePreview,
  srEnable, srHome, srLoc, srMark, srOpen, srPvSeq, srPvState, srRemoveFolder, srRes, srRow, srSave, srSecs, srSel,
  srSelect, srSeq, srShowPage, srStatusLine, srTimer, syncFiles, useFiles
};
