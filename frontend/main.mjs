// The UI's entry point: index.html loads it as a module. What is not split into frontend/ yet still lives here,
// in its original order (the refactor's P2 split, docs/refactor-progress.json).
import "./globals.mjs";
import {
  $, agAnswerMd, api, chDetach, confirmBox, esc, human, icon, jpost, lib, listState, loadChats, loadLibrary, newId,
  openChat, poll, renderThread, saveChat, scrollBottom, showView, store, threadState, toast, view
} from "./modules.mjs";

// ---------------- settings
async function loadManage() { loadStorage(); loadVersions(); loadSearch(); loadAgent(); loadMcp(); loadLibrary().then(() => {
  const inst = lib.chat.concat(lib.image).filter((m) => m.installed);
  $("modelsSum").textContent = `${inst.length} downloaded · ${human(inst.reduce((a, m) => a + m.size_bytes, 0))} on disk · ${human(lib.disk_free_bytes)} free`;
}); }
let mcpServers = [];
async function loadMcp() {
  let r; try { r = await api("/api/mcp/servers"); } catch { return; }
  mcpServers = r.servers || [];
  $("mcpSetSum").textContent = mcpServers.length ? `${mcpServers.length} configured server${mcpServers.length === 1 ? "" : "s"}` : "Custom Model Context Protocol stdio servers available in chat";
  $("mcpList").innerHTML = mcpServers.map((s) => `<div class="row" data-mcp="${esc(s.id)}">
    <div class="grow"><div class="ttl">${esc(s.id)} ${s.alive ? `<span class="badge ok" style="margin-left:6px">Connected</span>` : ""}</div>
    <div class="sub mono">${esc([s.command, ...(s.args || [])].join(" "))}</div></div>
    <button class="btn sm" data-mcptest="${esc(s.id)}">Test</button>
    <button class="btn sm danger" data-mcpdel="${esc(s.id)}">Remove</button>
  </div>`).join("");
}
$("mcpAdd").onclick = async () => {
  const id = $("mcpId").value.trim(), command = $("mcpCmd").value.trim(), argsRaw = $("mcpArgs").value.trim();
  if (!id || !command) return toast("Enter both a server ID and command");
  const args = argsRaw ? argsRaw.split(/\s+/).filter(Boolean) : [];
  try {
    await jpost("/api/mcp/servers", { id, command, args, enabled: true });
    $("mcpId").value = ""; $("mcpCmd").value = ""; $("mcpArgs").value = "";
    toast(`MCP server ${id} saved`);
    loadMcp();
  } catch (err) { toast(err.message); }
};
$("mcpList").onclick = async (e) => {
  const tb = e.target.closest("[data-mcptest]");
  if (tb) {
    const id = tb.dataset.mcptest;
    tb.disabled = true; tb.textContent = "Testing…";
    try {
      const r = await jpost(`/api/mcp/servers/${encodeURIComponent(id)}/test`);
      toast(`${id}: ${r.count || 0} tool(s) discovered`);
      loadMcp();
    } catch (err) { toast(err.message); tb.disabled = false; tb.textContent = "Test"; }
    return;
  }
  const db = e.target.closest("[data-mcpdel]");
  if (db) {
    const id = db.dataset.mcpdel;
    try {
      await api(`/api/mcp/servers/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast(`Removed ${id}`);
      loadMcp();
    } catch (err) { toast(err.message); }
  }
};
$("apiCopy").onclick = () => { navigator.clipboard.writeText($("apiUrl").textContent); toast("Copied"); };
async function loadVersions() {
  let v; try { v = await api("/api/versions"); } catch { return; }
  const rows = [["Local Studio", v.app], ["mlx-dspark", v["mlx-dspark"]], ["mlx-vlm", v["mlx-vlm"]], ["MLX", v.mlx], ["mflux", v.mflux], ["llama.cpp", v["llama.cpp"] || "not installed"]];
  $("verKvs").innerHTML = rows.map(([l, val]) => `<div><div class="l">${l}</div><div class="v">${esc(val || "–")}</div></div>`).join("");
  const u = v.update || {};
  $("updRow").style.display = u.status && u.status !== "idle" ? "" : "none";
  $("updBtn").disabled = u.status === "running";
  if (u.status === "running") { $("updStatus").textContent = `Updating… ${u.step || ""}`; setTimeout(loadVersions, 3000); }
  if (u.status === "done") { const ch = Object.entries(u.changed || {}); $("updStatus").textContent = ch.length ? "Updated: " + ch.map(([k, [a, b]]) => `${k} ${a || "–"} → ${b}`).join(", ") + ". Restart the engine to use the new versions." : "Everything is already up to date."; }
}
$("updBtn").onclick = async () => { try { await jpost("/api/versions/update"); toast("Checking for updates…"); loadVersions(); } catch (e) { toast(e.message); } };
const STORAGE = [["gallery", "Generated images", "Images in your library and their settings"], ["chats", "Conversations", "Saved chat history"], ["uploads", "Uploads", "Reference images you uploaded"], ["logs", "Logs", "Server and download logs"], ["engine_cache", "Engine calibration", "mlx-dspark speed calibration, rebuilt automatically"]];
async function loadStorage() {
  let s; try { s = await api("/api/storage"); } catch { return; }
  $("diskFree").textContent = `${human(s.disk_free_bytes)} free on this Mac`; $("unPath").textContent = `(${s.root})`;
  $("storageRows").innerHTML = STORAGE.map(([k, t, d]) => {
    const it = s[k] || {}; const cnt = it.count != null ? ` · ${it.count} item${it.count === 1 ? "" : "s"}` : "";
    return `<div class="row"><div class="grow"><div class="ttl">${t}</div><div class="sub">${d}${cnt}</div></div><div class="size">${human(it.bytes || 0)}</div><button class="btn sm" data-clear="${k}" ${it.bytes ? "" : "disabled"}>Clear</button></div>`;
  }).join("");
  $("storageRows").onclick = async (e) => {
    const b = e.target.closest("[data-clear]"); if (!b) return;
    const [, t] = STORAGE.find(([k]) => k === b.dataset.clear);
    if (!(await confirmBox(`Clear ${t.toLowerCase()}?`, "This permanently deletes the files and can't be undone.", "Clear"))) return;
    try { await jpost("/api/storage/clear", { what: b.dataset.clear }); toast(`${t} cleared`); } catch (err) { toast(err.message); }
    if (b.dataset.clear === "chats") { chDetach(); listState.current = null; loadChats(); }
    loadStorage();
  };
}
$("unConfirm").oninput = () => $("unBtn").disabled = $("unConfirm").value.trim().toLowerCase() !== "uninstall";
$("unBtn").onclick = async () => {
  const removeApp = $("unApp").checked;
  if (!(await confirmBox("Uninstall Local Studio?", `This removes the engines, the Mac app and every downloaded model${removeApp ? ", and deletes the app folder with your images and chats" : ""}. This page will stop working.`, "Uninstall"))) return;
  try { const r = await jpost("/api/uninstall", { remove_app: removeApp }); document.body.innerHTML = `<div style="height:100vh;display:grid;place-items:center;font:15px Inter,system-ui;color:#555;text-align:center;padding:24px"><div><h2 style="color:#111;font-weight:600">Uninstalling…</h2><p>Progress is written to <code>${esc(r.log)}</code>.<br>You can close this tab.</p></div></div>`; }
  catch (e) { toast(e.message); }
};

// ---------------- search
let sr = null, srTimer = 0, srSeq = 0, useFiles = store.get("useFiles", false); const mainState = { noFilesOnce: false };
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

// ---------------- agent
const AG_TITLE = "Local Studio", AG_CUA_MIN = "0.28.2", AG_CUA_URL = "https://github.com/trycua/cua";
const AG_EXAMPLES = [
  ["Research laptops", "Research the three best laptops under $1,000 and compare them in a table with prices and links."],
  ["Apple Store hours", "Find the opening hours of the Apple Store on Fifth Avenue in New York."],
  ["Fill a form", "Open https://httpbin.org/forms/post, fill in the form with sample details, and stop before submitting it."],
  ["Add to cart", "Find a USB-C cable on Amazon and add it to my cart. Don't check out."],
];
const AG_TIER = { read: "Read-only", ask: "Ask first", allowed: "Allowed" }, AG_TIER_ORDER = ["read", "ask", "allowed"];
const AG_STATUS = { done: "Finished", stopped: "Stopped", failed: "Failed", interrupted: "Interrupted", running: "Running", waiting: "Waiting for you", paused: "Paused" };
// One run's state. The Agent view keeps a single one (`ag`); a task started from chat keeps its own (`chRun.s`); every agXxx(s, …) below works on either.
function agFresh() {
  return { rid: null, events: [], last: -1, live: false, retries: 0, cards: {}, pending: null, paused: false, pausing: false, stopping: false, replyingN: null,
    engine: "", shot: null, where: "", urls: [], answer: "", task: "", status: "", reason: "", lost: false, chatId: null };
}
const ag = { ...agFresh(), st: null, runs: [], es: null, pinned: false, replay: false, timer: 0, shotShown: null, setSig: "", runsSig: "", bgWait: false,
  log: $("agLog"), scroller: () => document.querySelector("#v-agent .scroll"),
  hooks: { sync: () => agSync(), ended: () => { loadAgentRuns(); loadAgent(); }, retry: () => loadAgent() } };

const agPermMissing = (st) => st?.permissions?.accessibility === false || st?.permissions?.screen_recording === false;

// Pure helpers (also run under plain node by the author's checks).
// An approval title is markdown the server has already escaped: **bold** and \x escapes are all that is honoured, the rest is text.
function agTitleHTML(s) {
  const t = String(s ?? ""); let out = "", bold = false;
  for (let i = 0; i < t.length; i++) {
    const c = t[i];
    if (c === "\\" && i + 1 < t.length) { out += esc(t[++i]); continue; }
    if (c === "*" && t[i + 1] === "*") { out += bold ? "</strong>" : "<strong>"; bold = !bold; i++; continue; }
    out += esc(c);
  }
  return out + (bold ? "</strong>" : "");
}
// The run's pending step: the last approval or question that has no step of the same n after it (and no end).
function agPendingOf(events) {
  let p = null;
  for (const e of events) {
    if (e.type === "approval" || e.type === "question") p = e;
    else if (e.type === "step" && p && e.n === p.n) p = null;
    else if (e.type === "end") p = null;
  }
  return p;
}
function agPausedOf(events) {
  let paused = false;
  for (const e of events) {
    if (e.type === "note" && typeof e.text === "string") { if (e.text.startsWith("Paused while you take over")) paused = true; else if (e.text === "Resumed.") paused = false; }
    else if (e.type === "end") paused = false;
  }
  return paused;
}
const agBoxOf = (b) => Array.isArray(b) && b.length === 4 && b.every((v) => typeof v === "number" && isFinite(v)) && b[2] > 0 && b[3] > 0 ? b : null;
const agWeb = (u) => { try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x : null; } catch { return null; } };
const AG_TAGS = new Set("p br strong em b i del ul ol li a code pre blockquote h1 h2 h3 h4 h5 h6 table thead tbody tr th td hr".split(" "));
const AG_DROP = new Set("script style iframe object embed form input button textarea select svg math link meta base template noscript".split(" "));
function agClean(node) {
  for (const ch of [...node.childNodes]) {
    if (ch.nodeType === 3) continue;
    if (ch.nodeType !== 1) { ch.remove(); continue; }
    const tag = ch.localName;
    if (AG_DROP.has(tag)) { ch.remove(); continue; }
    if (tag === "img") { ch.replaceWith(document.createTextNode(ch.getAttribute("alt") || "")); continue; }   // model output never loads an image
    agClean(ch);
    if (!AG_TAGS.has(tag)) { ch.replaceWith(...ch.childNodes); continue; }
    const href = tag === "a" ? (ch.getAttribute("href") || "").trim() : "";
    for (const a of [...ch.attributes]) ch.removeAttribute(a.name);                // no on* handlers, styles or anything else
    if (tag === "a" && /^(https?:|mailto:)/i.test(href)) { ch.setAttribute("href", href); ch.setAttribute("target", "_blank"); ch.setAttribute("rel", "noopener noreferrer"); }
  }
}
// The final answer is model text: markdown, then only a short list of tags survives.
function agMd(s) {
  const html = window.marked ? marked.parse(String(s || "")) : esc(s).replace(/\n/g, "<br>");
  const t = document.createElement("template"); t.innerHTML = html;
  agClean(t.content);
  const d = document.createElement("div"); d.appendChild(t.content);
  return d.innerHTML;
}
const agShotUrl = (s, n) => `/api/agent/runs/${s.rid}/shots/${encodeURIComponent(n)}`;
// What a run ended with, from its events: the answer markdown (if any), the final status and reason.
function agOutcome(events, fallback = "") {
  let answer = "", status = "", reason = "";
  for (const e of events || []) {
    if (!e || typeof e !== "object") continue;
    if (e.type === "answer") answer = String(e.markdown || "");
    else if (e.type === "end") { status = e.status || ""; reason = e.reason || ""; }
  }
  return { answer, status: status || fallback, reason };
}
function agFinalText(o) {
  if (o.answer.trim()) return o.answer;
  if (o.status === "done") return "The agent finished without giving an answer.";
  return `The agent stopped: ${o.reason || (AG_STATUS[o.status] || o.status || "it was interrupted").toLowerCase()}`;
}
const agWhen = (t) => t ? new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
const agNoteEl = (text) => { const d = document.createElement("div"); d.className = "agnote"; d.textContent = text; return d; };
const agTierKey = (n) => {                       // like the server's _norm_label: no invisible or format characters, then whitespace, case
  const bare = (x) => x.replace(/\p{Cf}/gu, "").replace(/\p{Cc}/gu, " ");
  const k = bare(bare(String(n ?? "")).normalize("NFKC")).replace(/\s+/g, " ").trim().toLowerCase();
  return k.endsWith(".app") ? k.slice(0, -4) : k;
};

// ---- status, navigation, attention
async function agFetchStatus() {
  let st; try { st = await api("/api/agent/status"); } catch { return null; }
  ag.st = st;
  $("navAgent").hidden = true;
  return st;
}
async function loadAgent() {
  const st = await agFetchStatus(); if (!st) return;
  agRenderSettings(); agRenderSetup(); agSync();
  if (st.running && !ag.live && !ag.pinned && (st.running !== ag.rid || ag.lost)) agAttach(st.running);   // a task is in progress (or we lost it): follow it from its start
  else if (ag.lost && ag.rid && !st.running) agOpenSaved(ag.rid);                                         // it ended while we were away: show it as saved
  else if (st.running && ag.pinned && st.running !== ag.rid && !ag.live) agBgCheck(st.running);          // reading an old task while another waits for us
  else if (ag.bgWait) { ag.bgWait = false; agSync(); }
}
async function agBgCheck(rid) {
  let d; try { d = await api(`/api/agent/runs/${rid}`); } catch { return; }
  const evs = Array.isArray(d.events) ? d.events : [], wait = !!agPendingOf(evs) && !agPausedOf(evs);
  if (wait !== ag.bgWait) { ag.bgWait = wait; agSync(); }
}
function agOpen() {
  loadAgent(); loadAgentRuns(); loadLibrary();
  agSync();
}
setInterval(() => { if (view === "agent" || view === "manage") loadAgent(); }, 5000);
function agAttn(on) { document.title = on ? "⏸ A task needs your OK" : AG_TITLE; $("navAgent").classList.toggle("attn", on); }
// Needs the user: the Agent view's task, another task waiting in the background, or the task started from chat.
function agAttnNow() { const w = (s) => !!s && s.live && !!s.pending && !s.paused; agAttn(w(ag) || ag.bgWait || w(listState.chRun?.s)); }
// Shared by both places that show a run: which step waits for the user, whether the user holds the controls, and which cards still take an answer.
function agDerive(s) {
  s.pending = s.live ? agPendingOf(s.events) : null;
  s.paused = s.live && agPausedOf(s.events);
  if (s.paused) s.pausing = false;
  if (s.replyingN != null && s.pending?.n !== s.replyingN) s.replyingN = null;
  for (const [n, el] of Object.entries(s.cards)) {
    const active = !!s.pending && String(s.pending.n) === n;
    el.classList.toggle("done", !active);
    el.querySelectorAll(".agact button, .agact input").forEach((x) => { x.disabled = s.replyingN === s.pending?.n || (s.paused && x.dataset.agreply !== "stop"); });   // Stop run stays available while paused
  }
}
function agStateText(s, working, idle) {
  if (s.live) {
    if (s.paused) return ["Paused: you have control", "warn"];
    if (s.pending) return [s.pending.type === "question" ? "Waiting for your answer" : "Waiting for your OK", "warn"];
    if (s.stopping) return ["Stopping after this step…", "ok"];
    if (s.pausing) return ["Pausing…", "ok"];
    return [s.engine || working, "ok"];
  }
  if (s.status) return [AG_STATUS[s.status] || s.status, s.status === "done" ? "ok" : s.status === "failed" ? "bad" : ""];
  return [idle, ""];
}

// ---- the view
function agRenderModels() {
  const sel = $("agModel"); if (!lib) return;
  const inst = lib.chat.filter((m) => m.installed), cur = sel.value || store.get("agModel", "");
  sel.innerHTML = inst.length ? inst.map((m) => `<option value="${esc(m.id)}">${esc(m.name)} · ${m.vision ? "can read pages as images" : "text only"}</option>`).join("") : `<option value="">No chat model downloaded</option>`;
  const want = inst.some((m) => m.id === cur) ? cur : (inst.find((m) => m.active) || inst[0])?.id;
  if (want) sel.value = want;
  agSync();
}
function agRenderSetup() {
  const st = ag.st, box = $("agSetup"); if (!st) return;
  const c = st.cua || {}, link = (label) => `<a class="btn primary" href="${AG_CUA_URL}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  let t = "", m = "", act = "";
  if (!st.enabled) { t = "Agent is off"; m = "Turn it on to let the agent use a browser and Mac apps. Nothing runs until you start a task."; act = `<button class="btn primary" id="agSetupOn">Turn on Agent</button>`; }
  else if (!c.installed) { t = "CuaDriver is not installed"; m = "Agent needs CuaDriver to control the browser and apps. Install it from github.com/trycua/cua, then come back."; act = link("Get CuaDriver"); }
  else if (!c.ok_version) { t = "CuaDriver needs an update"; m = `You have ${c.version || "an unknown version"}. Agent needs ${AG_CUA_MIN} or later. Update it from github.com/trycua/cua.`; act = link("Update CuaDriver"); }
  else if (agPermMissing(st)) { t = "CuaDriver needs permissions"; m = "macOS hasn't given CuaDriver Accessibility and Screen Recording yet, so no task can start. Grant them, then start the task again."; act = `<button class="btn primary" data-agperm>Grant permissions</button>`; }
  box.style.display = t ? "" : "none";
  if (t) { $("agSetupTtl").textContent = t; $("agSetupMsg").textContent = m; $("agSetupAct").innerHTML = act; }
}
$("agSetup").onclick = async (e) => {
  if (e.target.closest("[data-agperm]")) return agGrant();
  if (!e.target.closest("#agSetupOn")) return;
  try { await jpost("/api/agent/enable", { on: true }); } catch (err) { toast(err.message); }
  loadAgent();
};
function agSetErr(msg) { const el = $("agErr"); el.textContent = msg || ""; el.hidden = !msg; }
function agSync() {
  const st = ag.st, live = ag.live, c = st?.cua || {};
  agDerive(ag);
  agAttnNow();
  $("agBack").style.display = ag.pinned && !live && st?.running && st.running !== ag.rid ? "" : "none";
  // state line
  const [txt, dot] = agStateText(ag, "Working…", "Ready");
  $("agState").textContent = txt; $("agDot").className = "dot " + dot;
  // live view
  const url = ag.shot && ag.rid ? agShotUrl(ag, ag.shot) : "";
  if (url !== ag.shotShown) {
    ag.shotShown = url;
    const box = $("agShotBox"); box.classList.toggle("has", !!url);
    box.innerHTML = url ? `<img src="${esc(url)}" alt="Latest screenshot">` : "The page or app the agent is working on shows here.";
  }
  $("agWhere").textContent = ag.where || "";
  $("agTakeover").disabled = !live || ag.pausing || ag.stopping;
  $("agTakeover").textContent = ag.paused ? "Resume" : ag.pausing ? "Pausing…" : "Take over";
  $("agStop").disabled = !live || ag.stopping;
  $("agStop").textContent = ag.stopping ? "Stopping…" : "Stop";
  // task box
  $("agTask").readOnly = live; $("agModel").disabled = live || !lib?.chat.some((m) => m.installed);
  const ready = !!st && st.enabled && c.installed && c.ok_version;
  $("agRun").disabled = live || !ready || !$("agTask").value.trim() || !$("agModel").value;
  $("agNew").style.display = !live && ag.events.length ? "" : "none";
  $("agChips").style.display = !live && !ag.events.length ? "" : "none";
  agRenderRuns();
}
$("agChips").innerHTML = AG_EXAMPLES.map(([label, q], i) => `<button data-i="${i}">${esc(label)}</button>`).join("");
$("agChips").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; $("agTask").value = AG_EXAMPLES[+b.dataset.i][1]; agSize(); agSync(); $("agTask").focus(); };
function agSize() { const t = $("agTask"); t.style.height = "auto"; t.style.height = Math.min(220, t.scrollHeight) + "px"; }
$("agTask").addEventListener("input", () => { agSize(); agSync(); });
$("agTask").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); agStart(); } });
$("agModel").onchange = () => { store.set("agModel", $("agModel").value); agSync(); };
$("agRun").onclick = () => agStart();
$("agBack").onclick = () => { if (ag.st?.running) { ag.pinned = false; agAttach(ag.st.running); } };
$("agNew").onclick = () => { agClear(); ag.pinned = false; $("agTask").value = ""; agSize(); agSync(); $("agTask").focus(); };

// ---- a run
function agClose(s) { if (s.es) { s.es.onmessage = s.es.onerror = s.es.onopen = null; s.es.close(); s.es = null; } clearTimeout(s.timer); }
function agClear() {
  agClose(ag);
  Object.assign(ag, agFresh(), { bgWait: false });
  $("agLog").innerHTML = ""; agSetErr("");
}
async function agStart() {
  const task = $("agTask").value.trim(), model_id = $("agModel").value;
  if (!task || !model_id || ag.live) return;
  $("agRun").disabled = true; agSetErr("");
  try {
    const r = await jpost("/api/agent/run", { task, model_id });
    store.set("agModel", model_id);
    agClear(); Object.assign(ag, { rid: r.run_id, live: true, pinned: false, task });
    agConnect(ag); loadAgentRuns();
  } catch (e) { agSetErr(e.message); toast(e.message); }
  agSync();
}
async function agAttach(rid) {
  agClear(); Object.assign(ag, { rid, live: true, pinned: false });
  agSync();
  try { const d = await api(`/api/agent/runs/${rid}`); if (ag.rid === rid) { ag.task = d.task || ""; ag.chatId = typeof d.chat_id === "string" ? d.chat_id : null; if (!$("agTask").value.trim()) { $("agTask").value = ag.task; agSize(); } } } catch {}
  if (ag.rid === rid && ag.live) agConnect(ag);
}
// One EventSource per (re)connect: the browser's own reconnect would replay from the original URL. `after` is inclusive.
function agConnect(s) {
  agClose(s);
  const rid = s.rid; if (!rid || !s.live) return;
  const es = new EventSource(`/api/agent/runs/${rid}/events?after=${s.last + 1}`);
  s.es = es;
  es.onopen = () => { s.retries = 0; };                          // the heartbeat is an SSE comment: an open stream is a healthy one
  es.onmessage = (m) => { let e; try { e = JSON.parse(m.data); } catch { return; } s.retries = 0; agEvent(s, e); };
  es.onerror = () => {
    if (s.es !== es) return;
    agClose(s);
    if (!s.live || s.rid !== rid) return;
    if (++s.retries > 8) { agLost(s); return; }
    s.timer = setTimeout(() => { if (s.live && s.rid === rid) agConnect(s); }, Math.min(5000, 700 * s.retries));
  };
}
function agLost(s) {                                  // keep trying: the retry hook reattaches as soon as the task can be reached, or shows it as saved
  s.live = false; s.lost = true; s.status = "interrupted";
  s.log.append(agNoteEl("Lost connection to the task. Trying to reconnect…"));
  s.hooks.sync();
  const again = () => { if (!s.lost) return; s.hooks.retry(); s.timer = setTimeout(again, 5000); };
  s.timer = setTimeout(again, 3000);
}
function agEvent(s, e) {
  if (!e || typeof e !== "object") return;
  if (typeof e.i === "number") { if (e.i <= s.last) return; s.last = e.i; }           // a replayed event is dropped
  const sc = s.scroller?.(), near = !!sc && !s.replay && sc.scrollHeight - sc.scrollTop - sc.clientHeight < 200;
  s.events.push(e);
  const before = s.log.lastElementChild;
  agRender(s, e);
  if (s.replay) return;
  s.hooks.sync();
  const card = (e.type === "approval" || e.type === "question") ? s.cards[e.n] : null, added = s.log.lastElementChild;
  if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  else if (added && added !== before && near) sc.scrollTop = sc.scrollHeight;
}
function agDecision(e) {
  const d = e.decision || "", failed = typeof e.result === "string" && /^(Error|Blocked)/.test(e.result);
  if (d === "blocked") return ["✕", "bad", "blocked"];
  if (d === "denied") return ["✕", "bad", "denied"];
  if (d === "skipped") return ["↻", "warn", "skipped"];
  if (d === "stopped") return ["⏸", "warn", "stopped"];
  if (d === "error") return ["↻", "warn", "retry"];
  if (failed) return ["✕", "bad", "failed"];
  if (d === "allowed") return ["✓", "ok", "approved"];
  return ["✓", "ok", ""];
}
function agRender(s, e) {
  const log = s.log;
  if (e.type === "x_engine") s.engine = e.status === "switching" ? `Loading ${e.model || "the model"}…` : "";
  else if (e.type === "step") agStepRow(s, e);
  else if (e.type === "approval") agApprovalCard(s, e);
  else if (e.type === "question") agQuestionCard(s, e);
  else if (e.type === "note") { log.append(agNoteEl(e.text || "")); if (/^Stopping/.test(e.text || "")) s.stopping = true; }
  else if (e.type === "answer") agAnswerCard(s, e);
  else if (e.type === "error") { const d = document.createElement("div"); d.className = "err agerr"; d.textContent = e.message || "Something went wrong."; log.append(d); }
  else if (e.type === "end") {
    Object.assign(s, { status: e.status, reason: e.reason || "", live: false, lost: false, stopping: false, pausing: false }); agClose(s);
    if (e.status !== "done") { const d = document.createElement("div"); d.className = "agend"; d.textContent = (AG_STATUS[e.status] || e.status) + (e.reason ? ` · ${e.reason}` : ""); log.append(d); }
    if (!s.replay) s.hooks.ended(s);
  }
}
function agStepRow(s, e) {
  const log = s.log;
  if (e.n == null) { log.append(agNoteEl(e.result || e.summary || "")); return; }
  const [ic, cls, tag] = agDecision(e), el = document.createElement("div");
  el.className = "agstep"; el.dataset.n = e.n; el._e = e;
  el.innerHTML = `<button class="agrow" data-agrow title="Show the screenshot and the call"><span class="agic ${cls}">${ic}</span><span class="agsum">${esc(e.summary || e.tool || "Step")}</span>${tag ? `<span class="agtag">${tag}</span>` : ""}</button>`
    + (e.thought ? `<details class="thought"><summary>Reasoning${icon("i-right")}</summary><div class="tb">${esc(e.thought)}</div></details>` : "");
  log.append(el);
  if (e.shot) s.shot = e.shot;
  const a = e.args && typeof e.args === "object" ? e.args : {}, visited = e.decision === "safe" || e.decision === "allowed";
  if (e.tool === "open_url" && typeof a.url === "string" && visited && !/^(Error|Blocked)/.test(e.result || "")) {
    s.where = a.url; if (agWeb(a.url) && !s.urls.includes(a.url)) s.urls.push(a.url);
  } else if ((e.tool === "open_app" || e.tool === "read_app") && typeof a.name === "string" && visited) s.where = "App: " + a.name;
  const card = s.cards[e.n]; if (card) agResolve(card, e);
}
function agDetail(s, e) {
  const d = document.createElement("div"); d.className = "agdet";
  const call = e.tool ? `${e.tool}(${e.args && Object.keys(e.args).length ? JSON.stringify(e.args, null, 1) : ""})` : "(no tool call)";
  d.innerHTML = (e.shot ? `<img loading="lazy" alt="Screenshot after this step" src="${esc(agShotUrl(s, e.shot))}">` : "")
    + `<div><div class="sub">Call</div><pre>${esc(call)}</pre></div><div><div class="sub">Result</div><pre>${esc(e.result || "")}</pre></div>`;
  return d;
}
function agCardEl(s, e, kind, inner) {
  const el = document.createElement("div"); el.className = "agcard"; el.dataset.n = e.n; el.dataset.kind = kind;
  el.innerHTML = inner; (s.cardBox || s.log).append(el); s.cards[e.n] = el; return el;
}
function agApprovalCard(s, e) {
  const box = agBoxOf(e.box);
  const el = agCardEl(s, e, "approval", `<div class="agcardh">⏸ Needs your OK</div><div class="agtitle">${agTitleHTML(e.title)}</div>`
    + (e.shot ? `<div class="agshot"><img alt="Screenshot of the page before this action" src="${esc(agShotUrl(s, e.shot))}">${box ? `<i class="agbox" hidden></i>` : ""}</div>` : "")
    + `<div class="agact"><button class="btn primary sm" data-agreply="allow">Allow once</button><button class="btn sm" data-agreply="deny">Deny</button><button class="btn sm danger" data-agreply="stop">Stop run</button></div><div class="agres"></div>`);
  const img = el.querySelector("img");
  if (e.shot) s.shot = e.shot;
  if (img && box) {
    const place = () => {                               // the outline is a share of the picture, so it follows the picture's size
      const W = img.naturalWidth, H = img.naturalHeight, o = el.querySelector(".agbox"); if (!W || !H || !o) return;
      const p = (v) => Math.max(0, Math.min(100, v)) + "%";
      Object.assign(o.style, { left: p(box[0] / W * 100), top: p(box[1] / H * 100), width: p(box[2] / W * 100), height: p(box[3] / H * 100) }); o.hidden = false;
    };
    if (img.complete && img.naturalWidth) place(); else img.addEventListener("load", place);
  }
}
function agQuestionCard(s, e) {
  const el = agCardEl(s, e, "question", `<div class="agcardh">⏸ The agent has a question</div><div class="agtitle"></div>`
    + `<div class="agact"><input placeholder="Your answer" autocomplete="off"><button class="btn primary sm" data-agsend>Send</button><button class="btn sm danger" data-agreply="stop">Stop run</button></div><div class="agres"></div>`);
  el.querySelector(".agtitle").textContent = e.text || "";
}
function agResolve(card, e) {
  const res = card.querySelector(".agres"), d = e.decision;
  res.textContent = card.dataset.kind === "question" ? (d === "safe" ? "You answered: " + String(e.result || "").replace(/^The user answered: /, "") : "Not answered.")
    : { allowed: "You allowed this.", denied: "You denied this.", skipped: "Skipped because you took over.", stopped: "Stopped." }[d] || "";
}
function agAnswerCard(s, e) {
  s.answer = String(e.markdown || "");
  if (s.answerCard === false) return;                    // a task started from chat shows its answer as the chat reply instead
  const pages = s.urls.slice(0, 8).map((u) => `<a class="chip" href="${esc(u)}" target="_blank" rel="noopener noreferrer" title="${esc(u)}">${esc(agWeb(u)?.host || u)}</a>`).join("");
  const el = document.createElement("div"); el.className = "agcard agans";
  el.innerHTML = `<div class="agcardh">Answer</div><div class="content">${agMd(s.answer)}</div>${pages ? `<div class="sources"><span class="sub" style="align-self:center">Pages used</span>${pages}</div>` : ""}`
    + `<div class="agact"><button class="btn primary sm" data-agcont>Continue in chat</button></div>`;
  s.log.append(el);
}
// Clicks and keys inside a run's log or cards (the Agent view's log, or a card in the chat thread).
function agLogClick(s, e) {
  const row = e.target.closest("[data-agrow]");
  if (row) { const step = row.closest(".agstep"); let det = step.querySelector(".agdet"); if (!det) { det = agDetail(s, step._e); step.insertBefore(det, step.querySelector(".thought")); } else det.hidden = !det.hidden; return; }
  if (s === ag && e.target.closest("[data-agcont]")) return agContinue();
  const card = e.target.closest(".agcard[data-n]"), n = card && +card.dataset.n;
  const r = e.target.closest("[data-agreply]"); if (r && card) return agReply(s, n, { decision: r.dataset.agreply });
  if (e.target.closest("[data-agsend]") && card) { const v = card.querySelector("input").value.trim(); if (v) agReply(s, n, { text: v }); }
}
function agLogKey(e) { if (e.key === "Enter" && e.target.matches(".agact input")) { e.preventDefault(); e.target.closest(".agcard").querySelector("[data-agsend]").click(); } }
$("agLog").onclick = (e) => agLogClick(ag, e);
$("agLog").addEventListener("keydown", agLogKey);
async function agReply(s, n, body) {
  const rid = s.rid; if (!rid || !s.live) return;
  s.replyingN = n; s.hooks.sync();
  try { await jpost(`/api/agent/runs/${rid}/reply`, { n, ...body }); }
  catch (err) {                                                      // maybe answered elsewhere or already past: show what the run really waits for
    toast(err.message); s.replyingN = null;
    try { const d = await api(`/api/agent/runs/${rid}`); if (s.rid === rid) (d.events || []).forEach((x) => agEvent(s, x)); } catch {}
    s.hooks.sync();
  }
}
async function agContinue() {
  if (listState.streaming) return toast("Wait for the reply to finish");
  if (!ag.answer) return;
  const task = ag.task || $("agTask").value.trim();
  if (ag.chatId) {                                            // a task that began in a chat: go back to that chat, which already has the answer
    let ok = false; try { await api(`/api/chats/${encodeURIComponent(ag.chatId)}`); ok = true; } catch {}
    if (ok) return openChat(ag.chatId);
  }
  chDetach();
  listState.current = { id: newId(), title: task.replace(/\s+/g, " ").slice(0, 48) || "Agent task", messages: [{ role: "user", content: task }, { role: "assistant", content: agAnswerMd(ag.answer) }] };
  await saveChat(); showView("chat"); renderThread(); scrollBottom(true);
}
async function agStopRun(s) {
  if (!s.rid || !s.live) return;
  s.stopping = true; s.hooks.sync();
  try { await jpost(`/api/agent/runs/${s.rid}/stop`); } catch (e) { toast(e.message); s.stopping = false; s.hooks.sync(); }
}
async function agTakeoverRun(s) {
  if (!s.rid || !s.live) return;
  const resume = s.paused; if (!resume) { s.pausing = true; s.hooks.sync(); }
  try { await jpost(`/api/agent/runs/${s.rid}/${resume ? "resume" : "takeover"}`); } catch (e) { toast(e.message); s.pausing = false; s.hooks.sync(); }
}
$("agStop").onclick = () => agStopRun(ag);
$("agTakeover").onclick = () => agTakeoverRun(ag);

// ---- recent tasks
async function loadAgentRuns() { try { ag.runs = await api("/api/agent/runs"); } catch { return; } agRenderRuns(); }
function agRenderRuns() {
  const sig = JSON.stringify([ag.runs.slice(0, 30), ag.rid]);
  if (sig === ag.runsSig) return;
  ag.runsSig = sig;
  $("agRecent").innerHTML = ag.runs.length ? ag.runs.slice(0, 30).map((r) => `<div class="agrun${r.id === ag.rid ? " on" : ""}" data-rid="${esc(r.id)}"><div class="grow"><div class="nm" title="${esc(r.task)}">${esc(r.task)}</div><div class="pt">${esc(agWhen(r.started))}</div></div><span class="badge${r.status === "done" ? " ok" : ""}">${esc(AG_STATUS[r.status] || r.status || "")}</span><button class="iconbtn" data-agdel title="Delete">${icon("i-trash")}</button></div>`).join("")
    : `<div class="hempty">Finished tasks appear here.</div>`;
}
$("agRecent").onclick = async (e) => {
  const row = e.target.closest(".agrun"); if (!row) return;
  const rid = row.dataset.rid;
  if (e.target.closest("[data-agdel]")) {
    if (!(await confirmBox("Delete this task?", "Its steps and screenshots are removed from this Mac.", "Delete"))) return;
    try { await api(`/api/agent/runs/${rid}`, { method: "DELETE" }); } catch (err) { return toast(err.message); }
    if (ag.rid === rid && !ag.live) { agClear(); agSync(); }
    return loadAgentRuns();
  }
  agOpenSaved(rid);
};
async function agOpenSaved(rid) {
  if (ag.rid === rid && !ag.lost) return;
  if (ag.live) return toast("Stop the running task first to open another");
  let d; try { d = await api(`/api/agent/runs/${rid}`); } catch (e) { return toast(e.message); }
  if (ag.st?.running === rid) return agAttach(rid);
  agClear(); Object.assign(ag, { rid, pinned: true, task: d.task || "", replay: true, chatId: typeof d.chat_id === "string" ? d.chat_id : null });
  $("agTask").value = ag.task; agSize();
  const evs = Array.isArray(d.events) ? d.events : [];
  evs.forEach((x) => agEvent(ag, x));
  if (!evs.some((x) => x.type === "end")) agEvent(ag, { i: evs.length, type: "end", status: d.status || "interrupted" });
  ag.replay = false; ag.live = false; ag.status = ag.status || d.status || "";
  agSync();
  document.querySelector("#v-agent .scroll").scrollTop = 0;
}

// ---- settings
function agRenderSettings() {
  const st = ag.st; if (!st) return;
  const c = st.cua || {}, s = st.settings || {}, perm = agPermMissing(st), ready = c.installed && c.ok_version && !perm;
  $("agEnable").checked = !!st.enabled;
  $("agSetSum").textContent = !st.enabled ? "Let chat take actions in your browser and Mac apps. You can also enable access when a chat task needs it." : ready ? "Chat can take actions when you ask, with approval before risky steps." : "Computer access is on, but CuaDriver needs attention.";
  $("agSetBody").style.display = st.enabled ? "" : "none";
  if (document.activeElement !== $("agSteps")) $("agSteps").value = s.max_steps ?? "";
  if (document.activeElement !== $("agMins")) $("agMins").value = s.max_minutes ?? "";
  const sig = JSON.stringify([st.enabled, c, s, st.permissions]);
  if (sig === ag.setSig) return;
  ag.setSig = sig;
  const link = `<a href="${AG_CUA_URL}" target="_blank" rel="noopener noreferrer">github.com/trycua/cua</a>`;
  let msg, act = "";
  if (!c.installed) msg = `Not installed. Get it from ${link}.`;
  else if (!c.ok_version) msg = `Update needed (have ${esc(c.version || "unknown")}, need ${AG_CUA_MIN}). Update it from ${link}.`;
  else if (perm) { msg = `v${esc(c.version)}, but macOS hasn't given it Accessibility and Screen Recording yet. No task can start until it has them.`; act = `<button class="btn sm" data-agperm>Grant permissions</button>`; }
  else { msg = `Ready v${esc(c.version)}. macOS asks once for Accessibility and Screen Recording.`; act = `<button class="btn sm" data-agperm>Grant permissions</button>`; }
  $("agCua").innerHTML = `<div class="grow"><div class="ttl">CuaDriver</div><div class="sub">${msg}</div></div>${act}`;
  const tel = $("agTelemetry"); tel.style.display = c.installed ? "" : "none";
  tel.innerHTML = `<div class="grow"><div class="ttl">Telemetry</div><div class="sub">${c.telemetry === true ? "On. CuaDriver may send usage data off this Mac." : c.telemetry === false ? "Off." : "Unknown."}</div></div>${c.telemetry === true ? `<button class="btn sm" data-agtele>Turn off</button>` : ""}`;
  agRenderTiers();
}
function agTierSelect(name, tier) {
  return `<div class="sel sm"><select data-agtier="${esc(name)}" title="${esc(name)}">${Object.entries(AG_TIER).map(([v, l]) => `<option value="${v}"${v === tier ? " selected" : ""}>${l}</option>`).join("")}</select></div>`;
}
function agRenderTiers() {
  const st = ag.st, defs = st.tier_defaults || {}, ov = st.settings?.tiers || {};
  const defByKey = new Map(Object.entries(defs).map(([n, t]) => [agTierKey(n), t]));
  const mineKeys = new Set(Object.keys(ov).map(agTierKey));
  const mine = Object.entries(ov).sort((a, b) => a[0].localeCompare(b[0]));
  const rest = Object.entries(defs).filter(([n]) => !mineKeys.has(agTierKey(n))).sort((a, b) => AG_TIER_ORDER.indexOf(a[1]) - AG_TIER_ORDER.indexOf(b[1]) || a[0].localeCompare(b[0]));
  const open = $("agTierList").querySelector("#agTierDefaults")?.open;
  $("agTierList").innerHTML = (mine.length ? `<div class="agtierh">Your changes</div>` + mine.map(([n, t]) => `<div class="agtier"><span class="nm" title="${esc(n)}">${esc(n)}</span>${agTierSelect(n, t)}<button class="btn sm" data-agtierrm="${esc(n)}">${defByKey.has(agTierKey(n)) ? "Use default" : "Remove"}</button></div>`).join("") : "")
    + `<details id="agTierDefaults"${open ? " open" : ""}><summary>Built-in apps (${rest.length})</summary>${rest.map(([n, t]) => `<div class="agtier"><span class="nm" title="${esc(n)}">${esc(n)}</span>${agTierSelect(n, t)}</div>`).join("")}</details>`;
}
async function agSave(patch) {
  try { await jpost("/api/agent/settings", patch, "PUT"); } catch (e) { toast(e.message); }
  ag.setSig = ""; loadAgent();
}
function agSetTier(name, tier) {                          // PUT replaces the whole map; a default app set back to its default needs no entry
  const ov = { ...(ag.st.settings.tiers || {}) }, key = agTierKey(name);
  for (const k of Object.keys(ov)) if (agTierKey(k) === key) delete ov[k];
  const def = Object.entries(ag.st.tier_defaults || {}).find(([n]) => agTierKey(n) === key);
  if (!(def && def[1] === tier)) ov[name.trim()] = tier;
  return agSave({ tiers: ov });
}
$("agTierList").onchange = (e) => { const s = e.target.closest("[data-agtier]"); if (s) agSetTier(s.dataset.agtier, s.value); };
$("agTierList").onclick = (e) => {
  const b = e.target.closest("[data-agtierrm]"); if (!b) return;
  const ov = { ...(ag.st.settings.tiers || {}) }, key = agTierKey(b.dataset.agtierrm);
  for (const k of Object.keys(ov)) if (agTierKey(k) === key) delete ov[k];
  agSave({ tiers: ov });
};
$("agTierAdd").onclick = async () => {
  const name = $("agTierName").value.trim().slice(0, 80); if (!name || !ag.st) return $("agTierName").focus();
  await agSetTier(name, $("agTierNew").value); $("agTierName").value = "";
};
$("agTierName").addEventListener("keydown", (e) => { if (e.key === "Enter") $("agTierAdd").click(); });
function agLimit(el, key, lo, hi, label) {
  el.onchange = () => {
    let v = parseInt(el.value, 10);
    if (!isFinite(v)) v = ag.st?.settings?.[key] ?? lo;
    const c = Math.max(lo, Math.min(hi, v));
    if (c !== v) toast(`${label} must be ${lo} to ${hi}`);
    el.value = c; agSave({ [key]: c });
  };
}
agLimit($("agSteps"), "max_steps", 1, 200, "Steps"); agLimit($("agMins"), "max_minutes", 1, 120, "Minutes");
$("agEnable").onchange = async () => {
  try { await jpost("/api/agent/enable", { on: $("agEnable").checked }); } catch (e) { toast(e.message); }
  ag.setSig = ""; loadAgent();
};
async function agGrant() {
  try { await jpost("/api/agent/permissions"); toast("Follow the macOS prompts for Accessibility and Screen Recording"); } catch (err) { toast(err.message); }
}
$("agCua").onclick = (e) => { if (e.target.closest("[data-agperm]")) agGrant(); };
$("agTelemetry").onclick = async (e) => {
  if (!e.target.closest("[data-agtele]")) return;
  if (!(await confirmBox("Turn off CuaDriver telemetry?", "This runs cua-driver telemetry disable, so CuaDriver stops sending usage data. It changes a CuaDriver setting, not a Local Studio one.", "Turn off"))) return;
  try { await jpost("/api/agent/telemetry/disable"); toast("Telemetry turned off"); } catch (err) { toast(err.message); }
  ag.setSig = ""; loadAgent();
};
$("agReset").onclick = async () => {
  if (!(await confirmBox("Reset the browser profile?", "The agent's browser forgets every site it signed in to and deletes its cookies. Your own browser is not touched.", "Reset"))) return;
  try { await jpost("/api/agent/profile/reset"); toast("Browser profile reset"); } catch (e) { toast(e.message); }
};

// ---------------- boot
(async () => {
  await api("/api/system").then((s) => { $("verBadge").textContent = s.version; threadState.sysRam = s.total_bytes; }).catch(() => {});
  await loadLibrary();
  loadSearch();
  loadAgent();
  showView("chat");
  renderThread();
  await loadChats();
  poll();
})();

export {
  ag, AG_CUA_MIN, AG_CUA_URL, AG_DROP, AG_EXAMPLES, AG_STATUS, AG_TAGS, AG_TIER, AG_TIER_ORDER, AG_TITLE, agAnswerCard,
  agApprovalCard, agAttach, agAttn, agAttnNow, agBgCheck, agBoxOf, agCardEl, agClean, agClear, agClose, agConnect,
  agContinue, agDecision, agDerive, agDetail, agEvent, agFetchStatus, agFinalText, agFresh, agGrant, agLimit,
  agLogClick, agLogKey, agLost, agMd, agNoteEl, agOpen, agOpenSaved, agOutcome, agPausedOf, agPendingOf, agPermMissing,
  agQuestionCard, agRender, agRenderModels, agRenderRuns, agRenderSettings, agRenderSetup, agRenderTiers, agReply,
  agResolve, agSave, agSetErr, agSetTier, agShotUrl, agSize, agStart, agStateText, agStepRow, agStopRun, agSync,
  agTakeoverRun, agTierKey, agTierSelect, agTitleHTML, agWeb, agWhen, loadAgent, loadAgentRuns, loadManage, loadMcp,
  loadSearch, loadStorage, loadVersions, mainState, mcpServers, runSearch, sr, SR_ICON, SR_OFF, SR_PAUSED, SR_STOP,
  srAddFolder, srAuto, srClosePreview, srEnable, srHome, srLoc, srMark, srOpen, srPvSeq, srPvState, srRemoveFolder,
  srRes, srRow, srSave, srSecs, srSel, srSelect, srSeq, srShowPage, srStatusLine, srTimer, STORAGE, syncFiles,
  useFiles
};
