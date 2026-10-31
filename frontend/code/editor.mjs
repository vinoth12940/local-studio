// code/editor.mjs: the Code view's editor and explorer. The editor is Monaco, the one VS Code is built on (colours, file
// tabs, find and replace, side-by-side diffs against the last commit); the explorer is a folder tree, the git changes and
// the open file's outline.
import { $, api, esc, human, jpost, toast } from "../core/util.mjs";
import { listState, renderHistory, sessionOf } from "../chat/list.mjs";
import { applyCodeLayout, codeUpdateStatus } from "../modules.mjs";
import { loadMonaco } from "./monaco.mjs";

// ---------------- code (workspace view: explorer · editor + terminal · agent chat)
let codeExTab = "files", codeOpenPath = "", codeOriginalContent = "", codeFileDiff = "", codeFullDiff = "", codeEdViewMode = "code", codeGitInfo = null, codeInstFiles = [], codeExSeq = 0;
const codeTabs = [];                                     // open files: { path, ws, model, saved, savedVersion, head, headModel, diff, lines, size, diag, view }
const codeTree = new Map(), codeOpenDirs = new Set();    // folder listings by path ("" is the workspace) and the expanded folders
let codeMon = null, codeEd = null, codeDiffEd = null, codeEdReady = null, codeTreeWs = null;
let codeCtxOff = "";                                     // the open file the user took off their messages (×); another file brings the chip back
const activeCodeWs = () => sessionOf("code")?.workspace || listState.codeWorkspace || "";      // Code's folder, never a Chat conversation's
const codeWsQ = (sep = "&") => activeCodeWs() ? `${sep}workspace=${encodeURIComponent(activeCodeWs())}` : "";
const codeTab = (path) => codeTabs.find((t) => t.path === path);
const codeTabDirty = (t) => !!t && t.model.getAlternativeVersionId() !== t.savedVersion;   // undoing back to the saved text is clean again
const codeDirty = () => codeTabs.some(codeTabDirty);
const CODE_CHECKED = /\.(py|json|js|mjs|cjs|swift)$/i;                                   // the file types _post_edit_diagnostics really checks
const CODE_MUTATING = ["write_file", "edit_file", "multi_edit_file", "run_shell", "run_python"];
const codeEmpty = (t, err = false) => `<div class="code-empty${err ? " err" : ""}">${esc(t)}</div>`;
const CODE_EDITOR_OPTIONS = {
  automaticLayout: true, theme: "local-studio", fontFamily: '"JetBrains Mono", ui-monospace, Menlo, monospace', fontSize: 12.5,
  lineHeight: 19, minimap: { enabled: true, renderCharacters: false }, scrollBeyondLastLine: false, smoothScrolling: true,
  renderLineHighlight: "all", stickyScroll: { enabled: true }, bracketPairColorization: { enabled: true },
  guides: { bracketPairs: true, indentation: true }, padding: { top: 8 }, fixedOverflowWidgets: true, detectIndentation: true
};

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

// Monaco loads on first use; the diff editor only when a diff is first shown. Edits in either side's modified file are
// edits to that tab's own model, so the tab, the Save button and the dirty dot follow both.
function codeEditorReady() {
  codeEdReady ||= loadMonaco().then((monaco) => {
    codeMon = monaco;
    codeEd = monaco.editor.create($("codeMonaco"), { ...CODE_EDITOR_OPTIONS, model: null });
    codeEd.onDidChangeCursorPosition(codeShowCursor);
    codeEd.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveCodeFile);
    return monaco;
  }).catch((err) => { codeEdReady = null; $("codeEdEmpty").textContent = err.message; $("codeEdEmpty").hidden = false; throw err; });
  return codeEdReady;
}
function codeDiffEditor() {
  if (!codeDiffEd) {
    codeDiffEd = codeMon.editor.createDiffEditor($("codeMonacoDiff"), { ...CODE_EDITOR_OPTIONS, minimap: { enabled: false },
      originalEditable: false, renderSideBySide: true, renderSideBySideInlineBreakpoint: 640, enableSplitViewResizing: true,
      ignoreTrimWhitespace: false });                    // side by side down to 640 px (Monaco's 900 is wider than this column usually is)
    codeDiffEd.getModifiedEditor().addCommand(codeMon.KeyMod.CtrlCmd | codeMon.KeyCode.KeyS, saveCodeFile);
  }
  return codeDiffEd;
}

function codeSyncDirty() {
  const d = codeTabDirty(codeTab(codeOpenPath));
  $("codeEdPath").classList.toggle("dirty", d);
  $("codeEdSave").disabled = !d;
  $("codeTabs").querySelectorAll("[data-tab]").forEach((el) => el.classList.toggle("dirty", codeTabDirty(codeTab(el.dataset.tab))));
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
function codeShowCursor() {
  const el = $("codeEdStatus"), m = codeEd?.getModel();
  if (!m || !codeTab(codeOpenPath) || codeEdViewMode !== "code") { el.textContent = ""; return; }
  const p = codeEd.getPosition() || { lineNumber: 1, column: 1 }, o = m.getOptions();
  const lang = codeMon.languages.getLanguages().find((l) => l.id === m.getLanguageId());
  el.textContent = [`Ln ${p.lineNumber}, Col ${p.column}`, o.insertSpaces ? `Spaces: ${o.indentSize}` : `Tab size: ${o.tabSize}`, "UTF-8",
    m.getEOL() === "\n" ? "LF" : "CRLF", lang?.aliases?.[0] || m.getLanguageId()].join("   ");
}
function codeMarkActive() {
  $("codeExList").querySelectorAll("[data-open-file]").forEach((el) => el.classList.toggle("on", el.dataset.openFile === codeOpenPath));
}
// The open file rides along with a message, as in Claude Code and Copilot: "@path" is added the first time this chat
// mentions it (the backend reads it in then), never again, so the same file is not sent with every turn.
function renderCodeCtx() {
  const box = $("codeCtx"), path = codeOpenPath;
  if (!path || /\s/.test(path)) { box.innerHTML = ""; return; }
  const off = codeCtxOff === path;
  box.innerHTML = `<span class="codechip${off ? " off" : ""}" title="${esc(off ? `${path} is not sent with your message` : `${path} goes with your next message`)}"><span class="nm">${esc(path.split("/").pop())}</span><button data-codectx="${off ? "on" : "off"}" aria-label="${off ? "Attach the open file" : "Don't send the open file"}">${off ? "+" : "×"}</button></span>`;
}
function codeToggleCtx(on) { codeCtxOff = on ? "" : codeOpenPath; renderCodeCtx(); }
function codeContextFor(text) {
  const path = codeOpenPath, mention = "@" + path;
  if (!path || /\s/.test(path) || codeCtxOff === path || !text || text.startsWith("/") || text.includes(mention)) return text;
  const said = (listState.current?.messages || []).some((m) => m.role === "user" && typeof m.content === "string" && m.content.includes(mention));
  return said ? text : `${mention} ${text}`;
}
function renderCodeTabs() {
  $("codeTabs").hidden = !codeTabs.length;
  $("codeTabs").innerHTML = codeTabs.map((t) => {
    const name = t.path.split("/").pop();
    return `<div class="code-tab${t.path === codeOpenPath ? " on" : ""}${codeTabDirty(t) ? " dirty" : ""}" data-tab="${esc(t.path)}" title="${esc(t.path)}"><span class="nm">${esc(name)}</span><button class="x" data-tab-close="${esc(t.path)}" title="Close" aria-label="Close ${esc(name)}">×</button></div>`;
  }).join("");
  $("codeTabs").querySelector(".code-tab.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function setCodeEditorMode(mode) {
  codeEdViewMode = mode === "diff" ? "diff" : "code";
  $("codeEdMode").querySelectorAll("[data-edmode]").forEach((b) => b.classList.toggle("on", b.dataset.edmode === codeEdViewMode));
  const tab = codeTab(codeOpenPath), diff = codeEdViewMode === "diff";
  if (diff && tab && codeMon) {                          // this file against its last commit, side by side; the right side stays editable
    tab.headModel ||= codeMon.editor.createModel(tab.head ?? "", tab.model.getLanguageId());
    codeDiffEditor().setModel({ original: tab.headModel, modified: tab.model });
  } else if (diff) $("codeDiffWrap").innerHTML = renderDiffLinesHTML(codeFullDiff, "this workspace");
  $("codeEdWrap").style.display = diff ? "none" : "";
  $("codeEdEmpty").hidden = !!tab;
  $("codeMonacoDiff").style.display = diff && tab ? "" : "none";
  $("codeDiffWrap").style.display = diff && !tab ? "" : "none";
  if (!tab) $("codeEdPath").textContent = diff ? "All uncommitted changes" : "No file open";
  codeShowCursor();
}

function codeActivate(path) {
  const prev = codeTab(codeOpenPath), tab = codeTab(path);
  if (prev && codeEd?.getModel() === prev.model) prev.view = codeEd.saveViewState();
  codeOpenPath = tab ? tab.path : "";
  codeOriginalContent = tab ? tab.saved : ""; codeFileDiff = tab ? tab.diff : "";
  if (codeEd) { codeEd.setModel(tab ? tab.model : null); if (tab?.view) codeEd.restoreViewState(tab.view); }
  $("codeEdPath").textContent = tab ? tab.path : "No file open"; $("codeEdPath").title = tab?.abs || "";
  $("codeEdMeta").textContent = tab ? `${tab.lines} lines · ${human(tab.size)}` : "";
  if (tab) codeShowDiag(tab.diag, tab.path); else $("codeEdDiag").style.display = "none";
  $("codeEdPin").disabled = !tab;
  renderCodeTabs(); codeSyncDirty(); codeMarkActive(); renderCodeCtx();
  setCodeEditorMode(codeEdViewMode);
  if (tab && codeEdViewMode === "code") codeEd?.focus();
  if (codeExTab === "outline") renderCodeExplorer();
}
// Reads a file into its tab (a new tab, or an open one that has no unsaved edits); null when it can't be read. A tab keeps
// the workspace it was read from, and is only ever saved there.
async function codeLoadTab(path, quiet, ws = activeCodeWs()) {
  let r;
  try { [r] = await Promise.all([api(`/api/workspace/file?path=${encodeURIComponent(path)}${ws ? `&workspace=${encodeURIComponent(ws)}` : ""}`), codeEditorReady()]); }
  catch (err) { if (!quiet) toast(err.message || `Could not open ${path}`); return null; }
  const content = r.content ?? "";
  let tab = codeTab(path);
  if (!tab) {
    tab = { path, ws, saved: content, view: null, headModel: null, model: codeMon.editor.createModel(content, undefined, codeMon.Uri.file(r.abs_path || "/" + path)) };
    tab.savedVersion = tab.model.getAlternativeVersionId();
    tab.model.onDidChangeContent(codeSyncDirty);
    codeTabs.push(tab);
  } else if (!codeTabDirty(tab) && tab.saved !== content) {
    tab.saved = content;
    tab.model.setValue(content);
    tab.savedVersion = tab.model.getAlternativeVersionId();
  }
  if (tab.head !== (r.head ?? null)) { tab.head = r.head ?? null; tab.headModel?.setValue(tab.head ?? ""); }
  Object.assign(tab, { abs: r.abs_path || path, diff: r.diff || "", lines: r.lines || 0, size: new Blob([content]).size, diag: r });
  return tab;
}
async function openCodeFile(path, jumpLine = 0, quiet = false) {
  if (!path || !(await codeLoadTab(path, quiet))) return false;
  codeActivate(path);
  if (jumpLine > 0) codeJump(jumpLine);
  return true;
}
function codeJump(line) {
  setCodeEditorMode("code");
  const m = codeEd?.getModel();
  if (!m) return;
  const n = Math.min(Math.max(1, line), m.getLineCount());
  codeEd.revealLineInCenter(n);
  codeEd.setSelection(new codeMon.Selection(n, 1, n, m.getLineMaxColumn(n)));
  codeEd.focus();
}
function codeCloseTab(path, force = false) {
  const i = codeTabs.findIndex((t) => t.path === path);
  if (i < 0) return true;
  const tab = codeTabs[i];
  if (!force && codeTabDirty(tab) && !confirm(`Discard unsaved changes to ${tab.path}?`)) return false;
  if (codeDiffEd?.getModel()?.modified === tab.model) codeDiffEd.setModel(null);     // a model is let go before it is disposed
  if (codeEd?.getModel() === tab.model) codeEd.setModel(null);
  tab.model.dispose(); tab.headModel?.dispose();
  codeTabs.splice(i, 1);
  if (path === codeOpenPath) codeActivate(codeTabs[Math.min(i, codeTabs.length - 1)]?.path || "");
  else renderCodeTabs();
  return true;
}
function codeCloseFile(label = "No file open") {                                      // every tab; callers ask about unsaved edits first
  for (const t of [...codeTabs]) codeCloseTab(t.path, true);
  codeActivate("");
  $("codeEdPath").textContent = label;
}
function codeConfirmDiscard() {
  const dirty = codeTabs.filter(codeTabDirty);
  return !dirty.length || confirm(`Discard unsaved changes to ${dirty.map((t) => t.path).join(", ")}?`);
}
function codeShowAllChanges() {
  codeActivate("");
  setCodeEditorMode("diff");
}
// After the agent or a save changed files: only the tab in view is read again (each read runs git and the syntax check);
// another tab is read again when it is clicked. Unsaved edits are left alone.
async function codeReloadTabs() {
  const t = codeTab(codeOpenPath);
  if (t && !codeTabDirty(t) && !(await codeLoadTab(t.path, true, t.ws))) codeCloseTab(t.path, true);   // deleted since
  codeActivate(codeOpenPath);
}
// Another workspace (a chat bound to another folder): the old folder's tabs close, and unsaved edits are saved to their
// own folder first if the user says so, never into the new one.
async function codeLeaveWorkspace() {
  for (const t of [...codeTabs]) {
    if (t.ws === activeCodeWs()) continue;
    if (codeTabDirty(t) && confirm(`Save your changes to ${t.path} (in ${t.ws || "the previous folder"}) before switching folders? Cancel discards them.`)) await codeSaveTab(t);
    codeCloseTab(t.path, true);
  }
}

async function codeSaveTab(tab) {
  const path = tab.path, content = tab.model.getValue(), version = tab.model.getAlternativeVersionId();
  const r = await jpost("/api/workspace/file", { path, content, workspace: tab.ws });
  if (r.error) throw new Error(r.error);
  Object.assign(tab, { saved: content, savedVersion: version, lines: r.lines || 0, size: new Blob([content]).size, diag: r });
  return r;
}
async function saveCodeFile() {
  const tab = codeTab(codeOpenPath);
  if (!codeTabDirty(tab)) return;
  const btn = $("codeEdSave"), path = tab.path;
  btn.disabled = true; btn.textContent = "Saving…";
  try {
    const r = await codeSaveTab(tab);
    if (path === codeOpenPath) codeOriginalContent = tab.saved;
    $("codeEdMeta").textContent = `${tab.lines} lines · saved`;
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
async function codeTreeLoad(dir) {
  const r = await api(`/api/workspace/tree?dir=${encodeURIComponent(dir)}${codeWsQ()}`);
  codeTree.set(dir, r.entries || []);
}
function codeTreeHTML(dir, depth) {
  return (codeTree.get(dir) || []).map((e) => {
    const pad = `style="padding-left:${6 + depth * 12}px"`;
    if (e.type === "dir") {
      const open = codeOpenDirs.has(e.path);
      return `<div class="code-ex-item dir${open ? " open" : ""}" data-toggle-dir="${esc(e.path)}" ${pad} title="${esc(e.path)}"><span class="chev"></span><span class="name">${esc(e.name)}</span></div>`
        + (open ? codeTreeHTML(e.path, depth + 1) : "");
    }
    return `<div class="code-ex-item file" data-open-file="${esc(e.path)}" ${pad} title="${esc(e.path)} · ${human(e.size || 0)}"><span class="chev none"></span><span class="name">${esc(e.name)}</span></div>`;
  }).join("");
}
async function codeToggleDir(dir) {
  if (codeOpenDirs.has(dir)) codeOpenDirs.delete(dir);
  else {
    codeOpenDirs.add(dir);
    if (!codeTree.has(dir)) {
      try { await codeTreeLoad(dir); }
      catch (err) { codeOpenDirs.delete(dir); toast(err.message); }
    }
  }
  renderCodeExplorer();
}
async function renderCodeExplorer() {
  const list = $("codeExList"), seq = ++codeExSeq, q = $("codeFileQ").value.trim(), ql = q.toLowerCase();
  if (codeExTab === "files" && q) {                       // a filter searches every folder: a flat list of matches
    let r;
    try { r = await api(`/api/workspace/files?q=${encodeURIComponent(q)}&limit=100${codeWsQ()}`); }
    catch (err) { if (seq === codeExSeq) list.innerHTML = codeEmpty(err.message, true); return; }
    if (seq !== codeExSeq) return;
    const items = (r.items || []).filter((f) => f.type === "file");
    list.innerHTML = items.length ? items.map((f) => {
      const i = f.path.lastIndexOf("/");
      return `<div class="code-ex-item" data-open-file="${esc(f.path)}" title="${esc(f.path)} · ${human(f.size || 0)}"><span class="name">${esc(f.path.slice(i + 1))}</span>${i > 0 ? `<span class="meta">${esc(f.path.slice(0, i))}</span>` : ""}</div>`;
    }).join("") : codeEmpty(`No files match “${q}”.`);
  } else if (codeExTab === "files") {                     // the folder tree: each folder is read when it is first opened
    try {
      if (!codeTree.has("")) await codeTreeLoad("");
      for (const dir of [...codeOpenDirs]) if (!codeTree.has(dir)) await codeTreeLoad(dir).catch(() => codeOpenDirs.delete(dir));
    } catch (err) { if (seq === codeExSeq) list.innerHTML = codeEmpty(err.message, true); return; }
    if (seq !== codeExSeq) return;
    list.innerHTML = codeTreeHTML("", 0) || codeEmpty("This folder has no files.");
    if (!codeTabs.length && codeEdViewMode === "code") {
      const root = codeTree.get("") || [], pick = ["AGENTS.md", "CLAUDE.md", "README.md"].find((n) => root.some((e) => e.path === n));
      if (pick) openCodeFile(pick, 0, true);
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
    if (info.workspace && !listState.codeWorkspace) { listState.codeWorkspace = info.workspace; renderHistory(); }
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
  if (codeTreeWs !== activeCodeWs()) {                                                 // another folder: its own tree and tabs
    await codeLeaveWorkspace();
    codeTreeWs = activeCodeWs(); codeTree.clear(); codeOpenDirs.clear();
  }
  else if (keepCurrentFile) codeTree.clear();                                          // a refresh reads the open folders again
  applyCodeLayout(); codeUpdateStatus();
  await renderCodeExplorer();
  if (keepCurrentFile && codeTabs.length) await codeReloadTabs();
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
  activeCodeWs, CODE_CHECKED, CODE_MUTATING, codeActivate, codeCloseFile, codeCloseTab, codeConfirmDiscard, codeContextFor,
  codeDirty, codeEdViewMode, codeEmpty, codeExSeq, codeExTab, codeFileDiff, codeFullDiff, codeGitInfo, codeInstFiles,
  codeJump, codeMarkActive, codeOpenPath, codeOriginalContent, codeSelectExTab, codeShowAllChanges, codeShowDiag,
  codeSyncDirty, codeToggleCtx, codeToggleDir, codeWsQ, loadCodeProcesses, loadCodeView, openCodeFile, renderCodeExplorer,
  renderDiffLinesHTML, saveCodeFile, setCodeEditorMode
};
