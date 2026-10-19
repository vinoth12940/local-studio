// code/editor.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { $, api, codeUpdateStatus, esc, human, jpost, listState, toast, updateWorkspaceUI } from "../modules.mjs";

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

export {
  activeCodeWs, CODE_CHECKED, CODE_MUTATING, codeCloseFile, codeConfirmDiscard, codeDirty, codeEdViewMode, codeEmpty,
  codeExSeq, codeExTab, codeFileDiff, codeFullDiff, codeGitInfo, codeInstFiles, codeJump, codeMarkActive, codeOpenPath,
  codeOriginalContent, codeSelectExTab, codeShowAllChanges, codeShowDiag, codeSyncDirty, codeWsQ, loadCodeProcesses,
  loadCodeView, openCodeFile, renderCodeExplorer, renderDiffLinesHTML, saveCodeFile, setCodeEditorMode,
  updateCodeGutter
};
