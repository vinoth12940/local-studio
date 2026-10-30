// code/terminal.mjs: moved from index.html's inline script in the refactor's P2 split.
import { $, api, esc, jpost, store, toast, vendorLoad } from "../core/util.mjs";
import { showView } from "../ui/views.mjs";
import { listState, newChat, saveChat, updateWorkspaceUI } from "../chat/list.mjs";
import { autosize, ctxUsage, fmtTok, toggleMode } from "../chat/composer.mjs";
import {
  activeCodeWs, codeActivate, codeCloseFile, codeCloseTab, codeConfirmDiscard, codeDirty, codeEdViewMode, codeEmpty, codeGitInfo,
  codeInstFiles, codeJump, codeOpenPath, codeSelectExTab, codeShowAllChanges, codeToggleCtx, codeToggleDir, loadCodeProcesses,
  loadCodeView, openCodeFile, renderCodeExplorer, saveCodeFile, setCodeEditorMode
} from "./editor.mjs";

// terminal: a real login shell on a pty (app.py "terminal"), drawn by xterm.js which loads on first use, from the copy
// bundled in frontend/vendor/ (VENDOR.json lists its source and hashes)
const XTERM_FILES = [
  ["link", "frontend/vendor/xterm/xterm.min.css", "sha384-tStR1zLfWgsiXCF3IgfB3lBa8KmBe/lG287CL9WCeKgQYcp1bjb4/+mwN6oti4Co"],
  ["script", "frontend/vendor/xterm/xterm.min.js", "sha384-J4qzUjBl1FxyLsl/kQPQIOeINsmp17OHYXDOMpMxlKX53ZfYsL+aWHpgArvOuof9"],
  ["script", "frontend/vendor/xterm/addon-fit.min.js", "sha384-XGqKrV8Jrukp1NITJbOEHwg01tNkuXr6uB6YEj69ebpYU3v7FvoGgEg23C1Gcehk"]];
let xtermLoad = null, codeTermS = null;                // codeTermS: { term, fit, id, ws, offset, ctrl, dead, inq, busy, rows, cols }
function loadXterm() {
  xtermLoad ||= vendorLoad(async () => {
    // Monaco's AMD loader (code/monaco.mjs) defines a global define.amd; xterm's files would register with it instead of
    // setting window.Terminal, so it is hidden while they run (never while Monaco loads: vendorLoad runs one job at a time).
    const amd = window.define?.amd;
    if (amd) delete window.define.amd;
    try {
      for (const [tag, url, sri] of XTERM_FILES) await new Promise((ok, fail) => {
        const el = document.createElement(tag);
        if (tag === "link") { el.rel = "stylesheet"; el.href = url; } else el.src = url;
        el.integrity = sri; el.crossOrigin = "anonymous"; el.onload = ok;
        el.onerror = () => fail(new Error("The terminal could not load its xterm.js files. Reinstall Local Studio to restore them."));
        document.head.appendChild(el);
      });
    } finally { if (amd) window.define.amd = amd; }
  }).catch((err) => { xtermLoad = null; throw err; });
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
  $("codeChatTitle").textContent = listState.current?.messages?.length ? listState.current.title || "Untitled chat" : "New chat";
  $("codeModelTxt").textContent = $("hdrSub").textContent || "Model";
}

// ---------------- layout: each pane sized and shown as the user leaves it (kept per browser, like the theme)
const CODE_LAYOUT_KEY = "code_layout";
function codeLayout() { return store.get(CODE_LAYOUT_KEY, {}) || {}; }
function applyCodeLayout() {
  const L = codeLayout(), wb = document.querySelector(".code-workbench");
  for (const [k, v] of [["--ex-w", L.ex], ["--chat-w", L.chat]]) wb.style.setProperty(k, v ? `${v}px` : "");   // "" removes it: the default width
  wb.classList.toggle("no-ex", !!L.hideEx); wb.classList.toggle("no-chat", !!L.hideChat);
  document.querySelector(".code-center").classList.toggle("no-term", !!L.hideTerm);
  $("codeTogEx").classList.toggle("on", !L.hideEx); $("codeTogTerm").classList.toggle("on", !L.hideTerm); $("codeTogChat").classList.toggle("on", !L.hideChat);
  setTimeout(codeTermFit, 0);
}
function setCodeLayout(patch) { store.set(CODE_LAYOUT_KEY, { ...codeLayout(), ...patch }); applyCodeLayout(); }
function codeSplitDrag(side) {                                                       // side: "ex" (the left divider) or "chat" (the right one)
  return (e) => {
    e.preventDefault();
    const wb = document.querySelector(".code-workbench"), rect = wb.getBoundingClientRect(), split = e.currentTarget;
    const other = side === "ex" ? document.querySelector(".code-chatpane") : document.querySelector(".code-explorer");
    const room = rect.width - (other.offsetParent ? other.getBoundingClientRect().width : 0) - 320;   // the editor keeps 320 px
    let width = 0;
    split.classList.add("drag"); document.body.style.cursor = "col-resize";
    window.onmousemove = (m) => {
      width = Math.round(Math.max(side === "ex" ? 140 : 260, Math.min(room, side === "ex" ? m.clientX - rect.left : rect.right - m.clientX)));
      wb.style.setProperty(side === "ex" ? "--ex-w" : "--chat-w", `${width}px`);
    };
    window.onmouseup = () => {
      window.onmousemove = window.onmouseup = null;
      split.classList.remove("drag"); document.body.style.cursor = "";
      if (width) setCodeLayout({ [side]: width });
    };
  };
}
$("codeSplitEx").onmousedown = codeSplitDrag("ex");
$("codeSplitChat").onmousedown = codeSplitDrag("chat");
$("codeSplitEx").ondblclick = () => setCodeLayout({ ex: 0 });
$("codeSplitChat").ondblclick = () => setCodeLayout({ chat: 0 });
$("codeTogEx").onclick = () => setCodeLayout({ hideEx: !codeLayout().hideEx });
$("codeTogTerm").onclick = () => setCodeLayout({ hideTerm: !codeLayout().hideTerm });
$("codeTogChat").onclick = () => setCodeLayout({ hideChat: !codeLayout().hideChat });
$("v-code").addEventListener("keydown", (e) => {                                     // VS Code's keys: ⌘B explorer, ⌘J terminal, ⌥⌘B chat
  if (!(e.metaKey || e.ctrlKey) || e.shiftKey) return;
  const k = e.key.toLowerCase(), patch = k === "b" ? (e.altKey ? "hideChat" : "hideEx") : k === "j" && !e.altKey ? "hideTerm" : "";
  if (!patch) return;
  e.preventDefault(); e.stopPropagation();
  setCodeLayout({ [patch]: !codeLayout()[patch] });
}, true);

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
$("codeChatHistory").onclick = () => ($("expand").style.display !== "none" ? $("expand") : $("collapse")).click();   // past chats are the sidebar's list
$("codeModelPill").onclick = (e) => { e.stopPropagation(); $("modelBtn").click(); };
$("codeCtx").onclick = (e) => { const b = e.target.closest("[data-codectx]"); if (b) codeToggleCtx(b.dataset.codectx === "on"); };
$("codeExToggle").onclick = (e) => { e.stopPropagation(); document.querySelector(".code-workbench").classList.toggle("ex-open"); };
$("v-code").addEventListener("mousedown", (e) => { if (!e.target.closest(".code-explorer, #codeExToggle")) document.querySelector(".code-workbench").classList.remove("ex-open"); });
$("codeExTabs").onclick = (e) => { const b = e.target.closest("[data-extab]"); if (b) codeSelectExTab(b.dataset.extab); };
$("codeFileQ").addEventListener("input", () => { clearTimeout(renderCodeExplorer._t); renderCodeExplorer._t = setTimeout(renderCodeExplorer, 120); });
$("codeExList").onclick = async (e) => {
  if (e.target.closest("[data-show-full-diff]")) return codeShowAllChanges();
  const dir = e.target.closest("[data-toggle-dir]");
  if (dir) return codeToggleDir(dir.dataset.toggleDir);
  const jump = e.target.closest("[data-jump-line]");
  if (jump) return codeJump(+jump.dataset.jumpLine || 1);
  const item = e.target.closest("[data-open-file]");
  if (item && (await openCodeFile(item.dataset.openFile))) { setCodeEditorMode(item.dataset.openDiff ? "diff" : codeEdViewMode); document.querySelector(".code-workbench").classList.remove("ex-open"); }
};
$("codeEdMode").onclick = (e) => { const b = e.target.closest("[data-edmode]"); if (b) setCodeEditorMode(b.dataset.edmode); };
$("codeTabs").onclick = (e) => {
  const x = e.target.closest("[data-tab-close]");
  if (x) { e.stopPropagation(); codeCloseTab(x.dataset.tabClose); return; }
  const tab = e.target.closest("[data-tab]");
  if (tab) openCodeFile(tab.dataset.tab, 0, true).then((ok) => ok || codeActivate(tab.dataset.tab));   // read it again if it has no unsaved edits
};
$("codeTabs").onauxclick = (e) => { const tab = e.target.closest("[data-tab]"); if (tab && e.button === 1) codeCloseTab(tab.dataset.tab); };   // middle click closes, as in VS Code
// The terminal panel's height: drag the line above it (kept per browser, like the theme).
$("codeTermResize").onmousedown = (e) => {
  e.preventDefault();
  const term = $("codeTerm"), startY = e.clientY, startH = term.getBoundingClientRect().height;
  const max = () => term.parentElement.getBoundingClientRect().height * 0.75;
  $("codeTermResize").classList.add("drag");
  window.onmousemove = (m) => { term.style.height = `${Math.round(Math.min(max(), Math.max(120, startH + startY - m.clientY)))}px`; codeTermFit(); };
  window.onmouseup = () => {
    window.onmousemove = window.onmouseup = null;
    $("codeTermResize").classList.remove("drag");
    store.set("code_term_h", term.style.height); codeTermFit();
  };
};
if (store.get("code_term_h")) $("codeTerm").style.height = store.get("code_term_h");
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
  const review = e.target.closest("[data-review-files]");
  if (review) {                                       // the turn's changed files, side by side against the last commit
    const names = review.dataset.reviewFiles.split("\n").filter(Boolean);
    const paths = (codeGitInfo?.files || []).map((f) => f.path.split(" -> ").pop()).filter((p) => names.includes(p.split("/").pop()));
    codeSelectExTab("changes");
    if (paths.length === 1 && (await openCodeFile(paths[0]))) setCodeEditorMode("diff");
    else codeShowAllChanges();
    return;
  }
  const stopBtn = e.target.closest("[data-proc-stop]");
  if (stopBtn) {
    try { await jpost(`/api/processes/${encodeURIComponent(stopBtn.dataset.procStop)}/stop`); toast("Process stopped"); loadCodeProcesses(); }
    catch (err) { toast(err.message); }
  }
});
window.onbeforeunload = (e) => { if (codeDirty()) { e.preventDefault(); e.returnValue = ""; } };

export {
  applyCodeLayout, codeTermFit, codeTermLabel, codeTermS, codeTermSend, codeTermStart, codeTermStream, codeTermTheme, codeUpdateStatus,
  loadXterm, onAgentCodeMutation, XTERM_FILES, xtermLoad
};
