// The UI's entry point: index.html loads it as a module. What is not split into frontend/ yet still lives here,
// in its original order (the refactor's P2 split, docs/refactor-progress.json).
import "./globals.mjs";
import {
  $, agAnswerMd, api, autosize, chDetach, chLive, closeMenus, compactCurrentChat, confirmBox, ctxUsage, engineState,
  esc, fmtTok, gib, human, icon, jpost, listState, loadChats, newChat, newId, openChat, poll, renderThread, saveChat,
  scrollBottom, send, showView, store, syncThink, threadState, toast, toggleMode, updateWorkspaceUI, view
} from "./modules.mjs";

// ---------------- images
$("advBtn").onclick = () => { $("adv").classList.toggle("on"); $("advBtn").classList.toggle("on"); };
$("dice").onclick = () => $("imgSeed").value = Math.floor(Math.random() * 2 ** 31);
$("imgPrompt").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(200, e.target.scrollHeight) + "px"; });
$("imgPrompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("imgGo").click(); });
let activeJob = null;
$("imgGo").onclick = async () => {
  const prompt = $("imgPrompt").value.trim(); if (!prompt) return $("imgPrompt").focus();
  const [w, h] = $("imgAspect").value.split("x"), fd = new FormData();
  Object.entries({ prompt, model_id: $("imgModel").value, width: w, height: h, steps: $("imgSteps").value, seed: $("imgSeed").value, guidance: $("imgGuidance").value, negative_prompt: $("imgNeg").value, step_cache: $("imgCache").value, mode: "generate" }).forEach(([k, v]) => fd.append(k, v));
  $("imgGo").disabled = true;
  try { const j = await api("/api/image", { method: "POST", body: fd }); activeJob = j.job; $("job").classList.add("on"); $("jobTitle").textContent = "Starting…"; $("jobSub").textContent = prompt; $("jobBar").style.width = "0"; $("jobPct").textContent = ""; track(j.job); }
  catch (e) { toast(e.message); $("imgGo").disabled = false; }
};
$("jobCancel").onclick = async () => { if (activeJob) { try { await jpost(`/api/image/${activeJob}/cancel`); toast("Cancelling…"); } catch (e) { toast(e.message); } } };
async function track(id) {
  let j; try { j = await api(`/api/image/${id}`); } catch { return setTimeout(() => track(id), 1500); }
  const pct = j.total ? j.step / j.total : 0;
  $("jobBar").style.width = (pct * 100) + "%"; $("jobPct").textContent = j.step ? `${Math.round(pct * 100)}%` : "";
  if (j.status === "queued") $("jobTitle").textContent = "Queued";
  if (j.status === "running") {
    let sub = `${Math.round(j.elapsed ?? 0)}s elapsed`;
    if (j.step > 1 && j.first_step_at) { const per = (Date.now() / 1000 - j.first_step_at) / (j.step - 1); sub += ` · about ${Math.max(1, Math.round(per * (j.total - j.step)))}s left · ${per.toFixed(1)}s per step`; }
    $("jobTitle").textContent = j.step ? `Generating · step ${j.step} of ${j.total}` : "Loading model…"; $("jobSub").textContent = sub;
  }
  if (["done", "error", "cancelled"].includes(j.status)) {
    $("job").classList.remove("on"); $("imgGo").disabled = false; activeJob = null;
    if (j.status === "done") { toast(`Image ready in ${Math.round(j.elapsed)}s`); loadGallery(); }
    if (j.status === "error") toast(`Generation failed: ${(j.log || []).slice(-1)[0] || "exit " + j.returncode}`);
    if (j.status === "cancelled") toast("Generation cancelled");
    return;
  }
  setTimeout(() => track(id), 1000);
}
let gallery = [];
async function loadGallery() {
  try { gallery = await api("/api/gallery"); } catch { gallery = []; }
  $("gCount").textContent = gallery.length ? `${gallery.length} image${gallery.length > 1 ? "s" : ""}` : "";
  $("gallery").innerHTML = gallery.length ? gallery.map((m, i) => `<div class="gitem" data-i="${i}"><img src="${m.url}" loading="lazy" alt="">
    <div class="tools"><a href="${m.url}" download title="Download" data-stop>${icon("i-dl")}</a><button data-del="${i}" title="Delete">${icon("i-trash")}</button></div>
    <div class="hov"><p>${esc(m.prompt)}</p><div class="s">${esc(m.model_name || "Qwen-Image 2.1")} · ${m.width}×${m.height} · ${m.steps} steps${m.elapsed ? " · " + Math.round(m.elapsed) + "s" : ""}</div></div></div>`).join("")
    : `<div class="empty">No images yet. Describe one above to get started.</div>`;
}
$("gallery").onclick = async (e) => {
  if (e.target.closest("[data-stop]")) return;
  const del = e.target.closest("[data-del]");
  if (del) { e.stopPropagation(); await deleteImage(gallery[+del.dataset.del]); return; }
  const it = e.target.closest(".gitem"); if (it) openLightbox(gallery[+it.dataset.i]);
};
async function deleteImage(m) {
  if ($("lbDlg").open) $("lbDlg").close();
  if (!(await confirmBox("Delete image?", "This image and its settings will be permanently deleted."))) return;
  await api(`/api/gallery/${encodeURIComponent(m.file)}`, { method: "DELETE" }); toast("Image deleted"); loadGallery();
}
let lbItem = null;
function openLightbox(m) {
  lbItem = m; $("lbImg").src = m.url; $("lbDl").href = m.url; $("lbPrompt").textContent = m.prompt;
  const rows = [["Model", m.model_name || "Qwen-Image 2.1"], ["Size", `${m.width} × ${m.height}`], ["Steps", m.steps], ["Seed", m.seed], ["Time", m.elapsed ? `${m.elapsed}s` : null], ["Per step", m.sec_per_step ? `${m.sec_per_step}s` : null],
    ["Peak memory", m.peak_bytes ? `${gib(m.peak_bytes)} GB` : null], ["Guidance", m.guidance], ["Step cache", m.step_cache_ratio], ["Created", m.created ? new Date(m.created * 1000).toLocaleString() : null]];
  $("lbKv").innerHTML = rows.filter(([, v]) => v != null && v !== "").map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  $("lbDlg").showModal();
}
$("lbClose").onclick = () => $("lbDlg").close();
$("lbDlg").onclick = (e) => { if (e.target === $("lbDlg")) $("lbDlg").close(); };
$("lbDel").onclick = () => deleteImage(lbItem);
$("lbReuse").onclick = () => {
  const m = lbItem; $("lbDlg").close();
  $("imgPrompt").value = m.prompt; $("imgSeed").value = m.seed; $("imgGuidance").value = m.guidance ?? ""; $("imgNeg").value = m.negative_prompt ?? ""; $("imgCache").value = m.step_cache_ratio ?? "";
  const size = `${m.width}x${m.height}`; if ([...$("imgAspect").options].some((o) => o.value === size)) $("imgAspect").value = size;
  if ([...$("imgSteps").options].some((o) => o.value == m.steps)) $("imgSteps").value = m.steps;
  $("imgPrompt").focus(); document.querySelector("#v-images .scroll").scrollTop = 0;
};

// ---------------- code (workspace view: explorer · editor + terminal · agent chat)
let codeExTab = "files", codeOpenPath = "", codeOriginalContent = "", codeFileDiff = "", codeFullDiff = "", codeEdViewMode = "code", codeGitInfo = null, codeInstFiles = [], codeExSeq = 0;
const activeCodeWs = () => listState.current?.workspace || listState.codeWorkspace || "";
const codeWsQ = (sep = "&") => activeCodeWs() ? `${sep}workspace=${encodeURIComponent(activeCodeWs())}` : "";
const codeDirty = () => !!codeOpenPath && $("codeEditor").value !== codeOriginalContent;
const CODE_CHECKED = /\.(py|json|js|mjs|cjs|swift)$/i;                                   // the file types _post_edit_diagnostics really checks
const CODE_MUTATING = ["write_file", "edit_file", "multi_edit_file", "run_shell", "run_python"];
const codeEmpty = (t, err = false) => `<div class="code-empty${err ? " err" : ""}">${esc(t)}</div>`;

function renderDiffLinesHTML(diffTxt, label) {
  if (!diffTxt || diffTxt === "(no diff)") return codeEmpty(`No uncommitted changes in ${label}.`);
  let header = false;
  return diffTxt.split("\n").map((line) => {
    let cls = "";
    if (line.startsWith("diff --git")) { header = true; cls = " file"; line = line.replace(/^diff --git a\/(.+?) b\/.*$/, "$1"); }
    else if (header && !line.startsWith("@@")) return "";                                // index/---/+++/mode lines of a file header
    else if (line.startsWith("@@")) { header = false; cls = " hunk"; }
    else if (line.startsWith("+")) cls = " add";
    else if (line.startsWith("-")) cls = " del";
    return `<span class="code-diff-line${cls}">${esc(line)}</span>`;
  }).join("");
}

function updateCodeGutter() {
  const ed = $("codeEditor"), gut = $("codeGutter");
  const n = Math.max(1, ed.value.split("\n").length);
  if (gut._lineCount !== n) {
    gut._lineCount = n;
    gut.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
  }
  gut.scrollTop = ed.scrollTop;
}
function codeSyncDirty() {
  const d = codeDirty();
  $("codeEdPath").classList.toggle("dirty", d);
  $("codeEdSave").disabled = !d;
}
function codeShowDiag(r, path) {
  const d = $("codeEdDiag"), chk = String(r?.syntax_check || "");
  if (!chk || chk.startsWith("skipped") || !CODE_CHECKED.test(path || "")) { d.style.display = "none"; return; }
  const ok = r.syntax_ok !== false;
  d.style.display = "";
  d.className = `badge ${ok ? "ok" : "warn"}`;
  d.textContent = ok ? "Syntax OK" : "Syntax error";
  d.title = chk;
}
function codeMarkActive() {
  $("codeExList").querySelectorAll("[data-open-file]").forEach((el) => el.classList.toggle("on", el.dataset.openFile === codeOpenPath));
}

function setCodeEditorMode(mode) {
  codeEdViewMode = mode === "diff" ? "diff" : "code";
  $("codeEdMode").querySelectorAll("[data-edmode]").forEach((b) => b.classList.toggle("on", b.dataset.edmode === codeEdViewMode));
  $("codeEdWrap").style.display = codeEdViewMode === "code" ? "" : "none";
  $("codeDiffWrap").style.display = codeEdViewMode === "diff" ? "" : "none";
  if (codeEdViewMode === "diff") $("codeDiffWrap").innerHTML = codeOpenPath ? renderDiffLinesHTML(codeFileDiff, codeOpenPath) : renderDiffLinesHTML(codeFullDiff, "this workspace");
}

function codeCloseFile(label = "No file open") {
  codeOpenPath = ""; codeOriginalContent = ""; codeFileDiff = "";
  const ed = $("codeEditor"); ed.value = ""; ed.disabled = true;
  $("codeEdPath").textContent = label; $("codeEdPath").title = "";
  $("codeEdMeta").textContent = ""; $("codeEdDiag").style.display = "none"; $("codeEdPin").disabled = true;
  updateCodeGutter(); codeSyncDirty(); codeMarkActive();
}
function codeConfirmDiscard() { return !codeDirty() || confirm(`Discard unsaved changes to ${codeOpenPath}?`); }

async function openCodeFile(path, jumpLine = 0, quiet = false) {
  if (!path) return false;
  if (path !== codeOpenPath && !codeConfirmDiscard()) return false;
  let r;
  try { r = await api(`/api/workspace/file?path=${encodeURIComponent(path)}${codeWsQ()}`); }
  catch (err) { if (!quiet) toast(err.message || `Could not open ${path}`); return false; }
  const ed = $("codeEditor"), same = path === codeOpenPath, top = ed.scrollTop;
  codeOpenPath = path;
  codeOriginalContent = r.content ?? "";
  codeFileDiff = r.diff || "";
  $("codeEdPath").textContent = path; $("codeEdPath").title = r.abs_path || path;
  $("codeEdMeta").textContent = `${r.lines || 0} lines · ${human(new Blob([codeOriginalContent]).size)}`;
  codeShowDiag(r, path);
  ed.disabled = false; ed.value = codeOriginalContent; ed.scrollTop = same ? top : 0;
  $("codeEdPin").disabled = false;
  updateCodeGutter(); codeSyncDirty(); codeMarkActive();
  if (codeEdViewMode === "diff") setCodeEditorMode("diff");
  if (jumpLine > 0) codeJump(jumpLine);
  if (codeExTab === "outline") renderCodeExplorer();
  return true;
}
function codeJump(line) {
  setCodeEditorMode("code");
  const ed = $("codeEditor"), lines = ed.value.split("\n"), n = Math.min(Math.max(1, line), lines.length);
  let pos = 0;
  for (let i = 0; i < n - 1; i++) pos += lines[i].length + 1;
  ed.focus();
  ed.setSelectionRange(pos, pos + lines[n - 1].length);
  ed.scrollTop = Math.max(0, (n - 6) * (parseFloat(getComputedStyle(ed).lineHeight) || 19.4));
  $("codeGutter").scrollTop = ed.scrollTop;
}
function codeShowAllChanges() {
  if (!codeConfirmDiscard()) return;
  codeCloseFile("All uncommitted changes");
  setCodeEditorMode("diff");
}

async function saveCodeFile() {
  if (!codeDirty()) return;
  const btn = $("codeEdSave"), path = codeOpenPath, content = $("codeEditor").value;
  btn.disabled = true; btn.textContent = "Saving…";
  try {
    const r = await jpost("/api/workspace/file", { path, content, workspace: activeCodeWs() });
    if (r.error) throw new Error(r.error);
    codeOriginalContent = content;
    $("codeEdMeta").textContent = `${r.lines || 0} lines · saved`;
    codeShowDiag(r, path);
    toast(r.syntax_ok === false ? `Saved with a syntax error: ${r.syntax_check}` : `Saved ${path}`);
    loadCodeView(true);
  } catch (err) { toast(err.message || "Save failed"); }
  finally { btn.textContent = "Save"; codeSyncDirty(); }
}

function codeSelectExTab(tab) {
  codeExTab = tab;
  $("codeExTabs").querySelectorAll("[data-extab]").forEach((b) => b.classList.toggle("on", b.dataset.extab === tab));
  $("codeFileQ").placeholder = tab === "outline" ? "Filter symbols…" : "Filter files…";
  renderCodeExplorer();
}
async function renderCodeExplorer() {
  const list = $("codeExList"), seq = ++codeExSeq, q = $("codeFileQ").value.trim(), ql = q.toLowerCase();
  if (codeExTab === "files") {
    let r;
    try { r = await api(`/api/workspace/files?q=${encodeURIComponent(q)}&limit=100${codeWsQ()}`); }
    catch (err) { if (seq === codeExSeq) list.innerHTML = codeEmpty(err.message, true); return; }
    if (seq !== codeExSeq) return;
    const items = (r.items || []).filter((f) => f.type === "file");
    list.innerHTML = items.length ? items.map((f) => {
      const i = f.path.lastIndexOf("/");
      return `<div class="code-ex-item" data-open-file="${esc(f.path)}" title="${esc(f.path)} · ${human(f.size || 0)}"><span class="name">${esc(f.path.slice(i + 1))}</span>${i > 0 ? `<span class="meta">${esc(f.path.slice(0, i))}</span>` : ""}</div>`;
    }).join("") : codeEmpty(q ? `No files match “${q}”.` : "This folder has no files.");
    if (!codeOpenPath && !q && codeEdViewMode === "code") {
      const pick = ["AGENTS.md", "CLAUDE.md", "README.md"].map((n) => items.find((f) => f.path === n)).find(Boolean);
      if (pick) openCodeFile(pick.path, 0, true);
    }
  } else if (codeExTab === "changes") {
    if (seq !== codeExSeq) return;
    const files = codeGitInfo?.files || [];
    if (codeGitInfo?.error) { list.innerHTML = codeEmpty("This folder is not a git repository."); return; }
    if (!files.length) { list.innerHTML = codeEmpty("Working tree is clean."); return; }
    const shown = ql ? files.filter((f) => f.path.toLowerCase().includes(ql)) : files;
    list.innerHTML = `<div class="code-ex-item all" data-show-full-diff="1"><span class="name">All changes</span><span class="meta r">${files.length}</span></div>` + shown.map((f) => {
      const st = f.status === "??" ? "U" : (f.status || "M").trim()[0];
      const path = f.path.includes(" -> ") ? f.path.split(" -> ").pop() : f.path, i = path.lastIndexOf("/");
      return `<div class="code-ex-item" data-open-file="${esc(path)}" data-open-diff="1" title="${esc(f.path)}"><span class="code-st ${esc(st)}">${esc(st)}</span><span class="name">${esc(path.slice(i + 1))}</span>${i > 0 ? `<span class="meta">${esc(path.slice(0, i))}</span>` : ""}</div>`;
    }).join("");
  } else {
    if (!codeOpenPath) { list.innerHTML = codeEmpty("Open a file to see its classes, functions and sections."); return; }
    list.innerHTML = codeEmpty("Reading symbols…");
    let r;
    try { r = await api(`/api/workspace/outline?path=${encodeURIComponent(codeOpenPath)}${codeWsQ()}`); }
    catch (err) { if (seq === codeExSeq) list.innerHTML = codeEmpty(err.message, true); return; }
    if (seq !== codeExSeq) return;
    const syms = (r.symbols || []).filter((s) => !ql || String(s.signature || s.name || "").toLowerCase().includes(ql));
    list.innerHTML = syms.length ? syms.map((s) => s.kind === "section"
      ? `<div class="code-ex-item sym-section" data-jump-line="${s.line}"><span class="name">${esc(s.name)}</span></div>`
      : `<div class="code-ex-item" data-jump-line="${s.line}" title="Line ${s.line}"><span class="name">${esc(s.signature || s.name || "")}</span><span class="meta r">${s.line}</span></div>`).join("")
      : codeEmpty(`No symbols found in ${codeOpenPath}.`);
  }
  codeMarkActive();
}

async function loadCodeView(keepCurrentFile = false) {
  let info = null;
  try { info = await api(`/api/workspace/info${codeWsQ("?")}`); }
  catch (err) { $("codeWsPath").textContent = err.message || "Could not load the workspace"; }
  if (info) {
    if (info.workspace && !listState.codeWorkspace) listState.codeWorkspace = info.workspace;
    if (listState.current && info.workspace && !listState.current.workspace) { listState.current.workspace = info.workspace; updateWorkspaceUI(); }
    const ws = info.workspace || "", name = ws.split("/").filter(Boolean).pop() || "Workspace";
    $("codeWsBadge").textContent = name;
    $("codeWsPick").title = `${ws} · choose another folder`;
    $("codeWsPath").innerHTML = `<bdi>${esc(ws)}</bdi>`; $("codeWsPath").title = ws;
    $("codeAutoReadonly").checked = info.auto_approve_readonly !== false;
    codeInstFiles = info.instructions || [];
    const inst = $("codeInstBadge");
    if (codeInstFiles.length) {
      inst.textContent = codeInstFiles.map((f) => f.name).join(" + ");
      inst.className = "badge ok";
      inst.title = `The agent reads these instructions every turn (${codeInstFiles.map((f) => `${f.name}: ${f.chars.toLocaleString()} chars`).join(", ")}). Click to open.`;
    } else {
      inst.textContent = "No AGENTS.md";
      inst.className = "badge";
      inst.title = "No AGENTS.md or CLAUDE.md in this folder. Click to draft one with /init.";
    }
    codeGitInfo = info.git_status || {};
    codeFullDiff = info.git_diff?.diff || "";
    const git = $("codeGitBadge"), cnt = (codeGitInfo.files || []).length;
    if (codeGitInfo.error) { git.textContent = "No git"; git.className = "badge"; }
    else {
      git.textContent = `${String(codeGitInfo.branch || "HEAD").split("...")[0]} · ${codeGitInfo.clean ? "clean" : `${cnt} changed`}`;
      git.className = `badge ${codeGitInfo.clean ? "ok" : "warn"}`;
    }
    $("codeChangeCnt").hidden = !cnt; $("codeChangeCnt").textContent = cnt;
  }
  codeUpdateStatus();
  await renderCodeExplorer();
  if (keepCurrentFile && codeOpenPath && !codeDirty()) { if (!(await openCodeFile(codeOpenPath, 0, true))) codeCloseFile(); }
  else if (!codeOpenPath && codeEdViewMode === "diff") setCodeEditorMode("diff");
  loadCodeProcesses();
}

async function loadCodeProcesses() {
  let r; try { r = await api("/api/processes"); } catch { return; }
  const procs = r.processes || [], running = procs.filter((p) => p.running).length;
  const badge = $("codeProcBadge");
  badge.style.display = running ? "" : "none"; badge.className = "badge ok"; badge.textContent = `${running} running`;
  const box = $("codeProcList");
  box.hidden = !procs.length;
  box.innerHTML = procs.map((p) => `<div class="code-proc"><span class="badge ${p.running ? "ok" : ""}">${p.running ? "Running" : `Exit ${p.returncode ?? 0}`}</span><span class="mono">${esc(p.process_id)}</span><span class="mono cmd" title="${esc(p.command)}">${esc(p.command)}</span>${p.running ? `<button class="btn sm danger" data-proc-stop="${esc(p.process_id)}">Stop</button>` : ""}</div>`).join("");
}

// terminal: a real login shell on a pty (app.py "terminal"), drawn by xterm.js which loads on first use
const XTERM_FILES = [
  ["link", "https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css", "sha384-tStR1zLfWgsiXCF3IgfB3lBa8KmBe/lG287CL9WCeKgQYcp1bjb4/+mwN6oti4Co"],
  ["script", "https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js", "sha384-J4qzUjBl1FxyLsl/kQPQIOeINsmp17OHYXDOMpMxlKX53ZfYsL+aWHpgArvOuof9"],
  ["script", "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js", "sha384-XGqKrV8Jrukp1NITJbOEHwg01tNkuXr6uB6YEj69ebpYU3v7FvoGgEg23C1Gcehk"]];
let xtermLoad = null, codeTermS = null;                // codeTermS: { term, fit, id, ws, offset, ctrl, dead, inq, busy, rows, cols }
function loadXterm() {
  xtermLoad ||= (async () => {
    for (const [tag, url, sri] of XTERM_FILES) await new Promise((ok, fail) => {
      const el = document.createElement(tag);
      if (tag === "link") { el.rel = "stylesheet"; el.href = url; } else el.src = url;
      el.integrity = sri; el.crossOrigin = "anonymous"; el.onload = ok;
      el.onerror = () => fail(new Error("The terminal could not load (it needs the internet once to fetch xterm.js)."));
      document.head.appendChild(el);
    });
  })().catch((err) => { xtermLoad = null; throw err; });
  return xtermLoad;
}
function codeTermTheme() {
  const cs = getComputedStyle(document.documentElement), v = (n) => cs.getPropertyValue(n).trim(), dark = cs.colorScheme === "dark";
  const pal = dark
    ? ["#3a3935", "#e5675a", "#5aae7a", "#e0b44a", "#6f9be8", "#c98bd6", "#5fb6c1", "#cdcac0", "#6f6c64", "#f08273", "#77c793", "#f0c96a", "#8fb2f0", "#dba6e4", "#7ccdd6", "#f0eee6"]
    : ["#1a1915", "#c2412d", "#2e7d4f", "#946200", "#2f5fb3", "#9b3f93", "#1d7480", "#d6d2c5", "#7d7a70", "#d6533e", "#3a9a62", "#ad7a00", "#3d71cc", "#b558ab", "#26939f", "#5d5a52"];
  const names = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
  const theme = { background: v("--code-bg"), foreground: v("--ink"), cursor: v("--brand"), cursorAccent: v("--code-bg"), selectionBackground: dark ? "#4a4944" : "#d6d2c5" };
  names.forEach((n, i) => { theme[n] = pal[i]; theme["bright" + n[0].toUpperCase() + n.slice(1)] = pal[i + 8]; });
  return theme;
}
async function codeTermStart(fresh = false) {
  const box = $("codeXterm");
  try { await loadXterm(); } catch (err) { box.innerHTML = codeEmpty(err.message, true); return; }
  if (!codeTermS) {
    const term = new Terminal({ fontFamily: '"JetBrains Mono", ui-monospace, Menlo, monospace', fontSize: 12.5, lineHeight: 1.2, cursorBlink: true,
      scrollback: 5000, macOptionIsMeta: true, theme: codeTermTheme() });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    box.innerHTML = "";
    term.open(box);
    codeTermS = { term, fit, id: null, ws: "", offset: 0, ctrl: null, dead: false, inq: "", busy: false };
    term.onData(codeTermSend);
    new ResizeObserver(codeTermFit).observe(box);
  }
  const S = codeTermS, ws = activeCodeWs();
  codeTermFit();
  if (S.id && !S.dead && !fresh) return;
  if (!fresh && !S.id) {                                                                // reattach to the shell from before a reload: its scrollback replays
    const saved = store.get("code_term", null);
    try {
      if (saved && (await api("/api/terminals")).terminals.some((t) => t.id === saved.id && t.running)) {
        Object.assign(S, { id: saved.id, ws: saved.ws, offset: 0, dead: false });
        codeTermLabel(); codeTermStream(); return;
      }
    } catch {}
  }
  if (S.id) { S.ctrl?.abort(); api(`/api/terminals/${S.id}`, { method: "DELETE" }).catch(() => {}); }
  S.term.reset();
  try {
    const r = await jpost("/api/terminals", { workspace: ws, rows: S.term.rows, cols: S.term.cols });
    Object.assign(S, { id: r.id, ws: r.cwd, offset: 0, dead: false, rows: S.term.rows, cols: S.term.cols });
    store.set("code_term", { id: r.id, ws: r.cwd });
    codeTermLabel(); codeTermStream();
  } catch (err) { S.dead = true; S.term.write(`\x1b[31m${err.message}\x1b[0m\r\n`); }
}
function codeTermLabel() {
  const ws = codeTermS?.ws || "";
  $("codeTermCwd").textContent = ws ? "· " + ws.replace(/^\/Users\/[^/]+/, "~") : "";
  $("codeTermCwd").title = ws;
}
async function codeTermStream() {                                                      // the server ends an idle stream after a few seconds; reconnect from the offset
  const S = codeTermS, id = S.id;
  let fails = 0;
  while (S.id === id && !S.dead) {
    const ctrl = new AbortController();
    S.ctrl = ctrl;
    try {
      const res = await fetch(`/api/terminals/${id}/stream?offset=${S.offset}`, { signal: ctrl.signal });
      if (res.status === 404) break;
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, i).split("\n").find((l) => l.startsWith("data: "));
          buf = buf.slice(i + 2);
          if (!line) continue;
          const j = JSON.parse(line.slice(6));
          if (j.d) { S.term.write(Uint8Array.from(atob(j.d), (c) => c.charCodeAt(0))); S.offset = j.o; }
          if (j.exit !== undefined && S.id === id) { S.dead = true; S.term.write(`\r\n\x1b[2m[shell exited${j.exit ? ` with code ${j.exit}` : ""} · press Enter for a new one]\x1b[0m\r\n`); }
        }
      }
      fails = 0;
    } catch {
      if (ctrl.signal.aborted) return;
      if (++fails > 2) break;
      await new Promise((r) => setTimeout(r, 800));
    }
  }
  if (S.id === id && !S.dead) { S.dead = true; S.term.write("\r\n\x1b[2m[disconnected · press Enter for a new shell]\x1b[0m\r\n"); }
}
function codeTermSend(data) {
  const S = codeTermS;
  if (!S?.id || S.dead) { if (S && data === "\r") codeTermStart(true); return; }
  S.inq += data;
  if (S.busy) return;
  S.busy = true;
  (async () => {                                                                        // one request at a time keeps keystrokes in order
    while (S.inq) {
      const chunk = S.inq; S.inq = "";
      try { await jpost(`/api/terminals/${S.id}/input`, { data: chunk }); } catch { S.inq = ""; break; }
    }
    S.busy = false;
  })();
}
function codeTermFit() {
  const S = codeTermS;
  if (!S || !$("codeXterm").offsetParent || $("codeTerm").classList.contains("collapsed")) return;
  try { S.fit.fit(); } catch { return; }
  if (S.id && !S.dead && (S.rows !== S.term.rows || S.cols !== S.term.cols)) {
    S.rows = S.term.rows; S.cols = S.term.cols;
    clearTimeout(S.rt);
    S.rt = setTimeout(() => jpost(`/api/terminals/${S.id}/resize`, { rows: S.rows, cols: S.cols }).catch(() => {}), 80);
  }
}
function codeUpdateStatus() {
  const parts = [];
  if (codeGitInfo && !codeGitInfo.error) parts.push(`⎇ ${String(codeGitInfo.branch || "HEAD").split("...")[0]}`);
  const u = ctxUsage();
  let ctx = "";
  if (u) {
    const pct = Math.min(100, Math.round(u.used / u.total * 100));
    ctx = `<span class="ctxs${pct >= 90 ? " bad" : pct >= 70 ? " warn" : ""}" title="${u.used.toLocaleString()} of ${u.total.toLocaleString()} tokens${u.measured ? "" : " (estimated)"}">ctx ${u.measured ? "" : "~"}${pct}% · ${fmtTok(Math.max(0, u.total - u.used))} free</span>`;
  }
  $("codeStatusTxt").innerHTML = [...parts.map(esc), ctx].filter(Boolean).join(" · ");
}

function onAgentCodeMutation() {                                                        // refresh after the agent writes or runs something; an unsaved editor is left alone
  clearTimeout(onAgentCodeMutation._t);
  onAgentCodeMutation._t = setTimeout(() => loadCodeView(true), 250);
}

$("codeWsPick").onclick = async () => {
  if (!codeConfirmDiscard()) return;
  try {
    const r = await jpost("/api/search/choose-folder");
    if (!r?.path) return;
    listState.codeWorkspace = r.path; store.set("code_workspace", r.path);
    if (listState.current) { listState.current.workspace = r.path; updateWorkspaceUI(); if (listState.current.messages.length) await saveChat(listState.current); }
    codeCloseFile(); $("codeFileQ").value = "";
    toast(`Workspace: ${r.path}`);
    loadCodeView(); codeTermStart(true);
  } catch (err) { toast(err.message || "Could not choose a folder"); }
};
$("codeInstBadge").onclick = () => {
  if (codeInstFiles.length) { setCodeEditorMode("code"); openCodeFile(codeInstFiles[0].name); return; }
  $("input").value = "/init"; autosize(); $("input").focus();
};
$("codeGitBadge").onclick = () => { if (codeGitInfo?.error) return; codeSelectExTab("changes"); codeShowAllChanges(); };
$("codeAutoReadonly").onchange = async () => {
  try {
    await jpost("/api/workspace/settings", { auto_approve_readonly: $("codeAutoReadonly").checked });
    toast($("codeAutoReadonly").checked ? "Read-only commands run without asking" : "Every command asks first");
  } catch (err) { toast(err.message); }
};
$("codeGitRefresh").onclick = () => loadCodeView(true);
$("codeOpenMcp").onclick = () => { showView("manage"); setTimeout(() => $("mcpSet")?.scrollIntoView({ behavior: "smooth" }), 80); };
$("codeNewTask").onclick = () => { newChat(); $("input").focus(); };
$("codeExToggle").onclick = (e) => { e.stopPropagation(); document.querySelector(".code-workbench").classList.toggle("ex-open"); };
$("v-code").addEventListener("mousedown", (e) => { if (!e.target.closest(".code-explorer, #codeExToggle")) document.querySelector(".code-workbench").classList.remove("ex-open"); });
$("codeExTabs").onclick = (e) => { const b = e.target.closest("[data-extab]"); if (b) codeSelectExTab(b.dataset.extab); };
$("codeFileQ").addEventListener("input", () => { clearTimeout(renderCodeExplorer._t); renderCodeExplorer._t = setTimeout(renderCodeExplorer, 120); });
$("codeExList").onclick = async (e) => {
  if (e.target.closest("[data-show-full-diff]")) return codeShowAllChanges();
  const jump = e.target.closest("[data-jump-line]");
  if (jump) return codeJump(+jump.dataset.jumpLine || 1);
  const item = e.target.closest("[data-open-file]");
  if (item && (await openCodeFile(item.dataset.openFile))) { setCodeEditorMode(item.dataset.openDiff ? "diff" : codeEdViewMode); document.querySelector(".code-workbench").classList.remove("ex-open"); }
};
$("codeEdMode").onclick = (e) => { const b = e.target.closest("[data-edmode]"); if (b) setCodeEditorMode(b.dataset.edmode); };
$("codeEditor").addEventListener("input", () => { updateCodeGutter(); codeSyncDirty(); });
$("codeEditor").addEventListener("scroll", () => { $("codeGutter").scrollTop = $("codeEditor").scrollTop; });
$("codeEditor").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveCodeFile(); return; }
  if (e.key === "Tab" && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); document.execCommand("insertText", false, "  "); }   // keeps native undo
});
$("codeEdSave").onclick = saveCodeFile;
$("codeEdPin").onclick = () => {
  if (!codeOpenPath) return;
  const cur = $("input").value;
  $("input").value = (cur && !/\s$/.test(cur) ? cur + " " : cur) + `@${codeOpenPath} `;
  autosize(); $("input").focus();
};
$("codeTermToggle").onclick = () => { const c = $("codeTerm").classList.toggle("collapsed"); $("codeTermToggle").textContent = c ? "Show" : "Hide"; if (!c) setTimeout(codeTermFit, 0); };
$("codeTermClear").onclick = () => { codeTermS?.term.clear(); codeTermS?.term.focus(); };
$("codeTermNew").onclick = () => codeTermStart(true).then(() => codeTermS?.term.focus());
$("codeModeBtn").onclick = toggleMode;
$("v-code").addEventListener("click", async (e) => {
  const cmdBtn = e.target.closest("[data-code-cmd]");
  if (cmdBtn) {
    if (listState.streaming || chLive()) return toast("Wait for the reply to finish");
    const cmd = cmdBtn.dataset.codeCmd;
    if (cmd === "/compact") return compactCurrentChat();
    const ws = activeCodeWs();
    if (!listState.current) listState.current = { id: newId(), title: cmd, messages: [] };
    if (ws) listState.current.workspace = ws;
    $("input").value = cmd; autosize(); send();
    return;
  }
  const subBtn = e.target.closest("[data-code-subagent]");
  if (subBtn) {
    const prompts = {
      researcher: "Use delegate_task with role \"researcher\" to explore this workspace and summarize its architecture and key modules.",
      verifier: "Use delegate_task with role \"verifier\" to inspect the recent changes and run the project's verification checks."
    };
    $("input").value = prompts[subBtn.dataset.codeSubagent] || prompts.researcher;
    autosize(); $("input").focus();
    return;
  }
  const stopBtn = e.target.closest("[data-proc-stop]");
  if (stopBtn) {
    try { await jpost(`/api/processes/${encodeURIComponent(stopBtn.dataset.procStop)}/stop`); toast("Process stopped"); loadCodeProcesses(); }
    catch (err) { toast(err.message); }
  }
});
window.onbeforeunload = (e) => { if (codeDirty()) { e.preventDefault(); e.returnValue = ""; } };

// ---------------- library
let lib = null, libTab = "chat"; const mainState = { runningName: null, runningMode: null };
const activeChat = () => lib?.chat.find((m) => m.active);
const activeImage = () => lib?.image.find((m) => m.active);
function updateHeader() {
  if (view === "chat") {
    const a = activeChat();
    $("hdrTitle").textContent = mainState.runningName || a?.name || "Local Studio";
    $("hdrSub").textContent = mainState.runningMode === "vision" ? "Vision" : mainState.runningMode === "gguf" ? "llama.cpp" : (a?.accel || "");
  } else if (view === "code") { $("hdrTitle").textContent = "Code"; $("hdrSub").textContent = mainState.runningName || activeChat()?.name || ""; }
  else if (view === "images") { $("hdrTitle").textContent = activeImage()?.name || "Images"; $("hdrSub").textContent = "mflux"; }
  else if (view === "library") { $("hdrTitle").textContent = "Model library"; $("hdrSub").textContent = ""; }
  else if (view === "search") { $("hdrTitle").textContent = "Search"; $("hdrSub").textContent = "EmbeddingGemma 2"; }
  else if (view === "agent") { $("hdrTitle").textContent = "Agent"; $("hdrSub").textContent = ""; }
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
let sr = null, srTimer = 0, srSeq = 0, useFiles = store.get("useFiles", false); mainState.noFilesOnce = false;
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
  activeChat, activeCodeWs, activeImage, activeJob, ag, AG_CUA_MIN, AG_CUA_URL, AG_DROP, AG_EXAMPLES, AG_STATUS,
  AG_TAGS, AG_TIER, AG_TIER_ORDER, AG_TITLE, agAnswerCard, agApprovalCard, agAttach, agAttn, agAttnNow, agBgCheck,
  agBoxOf, agCardEl, agClean, agClear, agClose, agConnect, agContinue, agDecision, agDerive, agDetail, agEvent,
  agFetchStatus, agFinalText, agFresh, agGrant, agLimit, agLogClick, agLogKey, agLost, agMd, agNoteEl, agOpen,
  agOpenSaved, agOutcome, agPausedOf, agPendingOf, agPermMissing, agQuestionCard, agRender, agRenderModels,
  agRenderRuns, agRenderSettings, agRenderSetup, agRenderTiers, agReply, agResolve, agSave, agSetErr, agSetTier,
  agShotUrl, agSize, agStart, agStateText, agStepRow, agStopRun, agSync, agTakeoverRun, agTierKey, agTierSelect,
  agTitleHTML, agWeb, agWhen, cardHTML, cardSig, CODE_CHECKED, CODE_MUTATING, codeCloseFile, codeConfirmDiscard,
  codeDirty, codeEdViewMode, codeEmpty, codeExSeq, codeExTab, codeFileDiff, codeFullDiff, codeGitInfo, codeInstFiles,
  codeJump, codeMarkActive, codeOpenPath, codeOriginalContent, codeSelectExTab, codeShowAllChanges, codeShowDiag,
  codeSyncDirty, codeTermFit, codeTermLabel, codeTermS, codeTermSend, codeTermStart, codeTermStream, codeTermTheme,
  codeUpdateStatus, codeWsQ, deleteImage, dlText, gallery, hfKind, hfSearch, lbItem, lib, libTab, loadAgent,
  loadAgentRuns, loadCodeProcesses, loadCodeView, loadGallery, loadLibrary, loadManage, loadMcp, loadSearch,
  loadStorage, loadVersions, loadXterm, mainState, mcpServers, onAgentCodeMutation, openCodeFile, openLightbox,
  patchCards, renderCodeExplorer, renderDiffLinesHTML, renderImgModels, renderLibrary, renderMenu, runSearch,
  saveCodeFile, setActive, setCodeEditorMode, setStepOptions, sr, SR_ICON, SR_OFF, SR_PAUSED, SR_STOP, srAddFolder,
  srAuto, srClosePreview, srEnable, srHome, srLoc, srMark, srOpen, srPvSeq, srPvState, srRemoveFolder, srRes, srRow,
  srSave, srSecs, srSel, srSelect, srSeq, srShowPage, srStatusLine, srTimer, STORAGE, syncFiles, tagsHTML, track,
  updateCodeGutter, updateHeader, useFiles, XTERM_FILES, xtermLoad
};
