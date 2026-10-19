// The UI's entry point: index.html loads it as a module. What is not split into frontend/ yet still lives here,
// in its original order (the refactor's P2 split, docs/refactor-progress.json).
import "./globals.mjs";

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
  if (!r.ok) throw new Error((j && (j.detail || j.error)) || `HTTP ${r.status}`);
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
if (window.marked) marked.use({ renderer: { html(h) { return esc(typeof h === "string" ? h : h.text); } } });
// Model and page text reaches this renderer (and saved chats re-render through it), so what it keeps is an allowlist:
// links only to http(s), mailto and in-page anchors; images only from this app's own /api/ and /gallery/; no active elements.
const CHAT_DROP = new Set("script style iframe object embed form base meta link svg math template noscript frame frameset applet".split(" "));
function cleanChat(root) {
  for (const el of [...root.querySelectorAll("*")]) {
    const tag = el.localName;
    if (CHAT_DROP.has(tag)) { el.remove(); continue; }
    for (const a of [...el.attributes]) if (/^on/i.test(a.name) || ["style", "srcset", "formaction", "xlink:href"].includes(a.name.toLowerCase())) el.removeAttribute(a.name);
    if (tag === "a") {
      const href = (el.getAttribute("href") || "").trim();
      if (!/^(https?:|mailto:|#)/i.test(href)) el.replaceWith(...el.childNodes);
      else if (!href.startsWith("#")) { el.setAttribute("target", "_blank"); el.setAttribute("rel", "noopener noreferrer"); }
    } else if (tag === "img" && !/^\/(api|gallery)\//.test((el.getAttribute("src") || "").trim())) {
      const alt = el.getAttribute("alt"), sp = document.createElement("span");
      sp.textContent = alt ? `[image: ${alt}]` : "[image]"; el.replaceWith(sp);
    }
  }
}
// The same rule on markdown text, so a stored chat does not carry a hostile link or image from the start.
function safeMd(s) {
  const first = (u) => u.trim().replace(/^<|>$/g, "").split(/\s+/)[0], url = "\\(((?:[^()]|\\([^()]*\\))*)\\)";   // one level of ( ) inside the address
  return String(s || "")
    .replace(new RegExp("!\\[([^\\]]*)\\]" + url, "g"), (m, alt, u) => /^\/(api|gallery)\//.test(first(u)) ? m : `[image: ${alt}]`)
    .replace(new RegExp("(?<!!)\\[([^\\]]*)\\]" + url, "g"), (m, t, u) => /^(https?:|mailto:|#)/i.test(first(u)) ? m : t)
    .replace(/^(\s{0,3}\[[^\]]+\]:\s*)(\S+)/gm, (m, a, u) => /^(https?:|mailto:|#)/i.test(u.replace(/^</, "")) ? m : a + "#");
}
// Answers from the Agent are page-steered text: they never carry an image into a chat (a same-origin one would load on every open).
function agNoImages(s) {                                     // a "!" before "[" is what makes an image: without it every form is a link or plain text, in one pass and again on any later one
  return String(s || "").replace(/!+(?=\[)/g, "");
}
const agAnswerMd = (s) => safeMd(agNoImages(s));
function md(s) {
  const html = window.marked ? marked.parse(s || "") : esc(s).replace(/\n/g, "<br>");
  const t = document.createElement("template"); t.innerHTML = html; cleanChat(t.content);   // a template is inert: nothing loads or runs while it is cleaned
  const tmp = document.createElement("div"); tmp.appendChild(t.content);
  tmp.querySelectorAll("pre > code").forEach((code) => {
    const lang = (code.className.match(/language-([\w+#-]+)/) || [])[1] || "code";
    const wrap = document.createElement("div"); wrap.className = "codeblock";
    wrap.innerHTML = `<div class="ch"><span>${esc(lang)}</span><button data-copycode>${icon("i-copy", "width:14px;height:14px")}Copy</button></div>`;
    const pre = code.parentElement; pre.replaceWith(wrap); wrap.appendChild(pre);
  });
  return tmp.innerHTML;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copycode]");
  if (b) { navigator.clipboard.writeText(b.closest(".codeblock").querySelector("code").innerText); b.innerHTML = `${icon("i-check", "width:14px;height:14px")}Copied`; setTimeout(() => b.innerHTML = `${icon("i-copy", "width:14px;height:14px")}Copy`, 1500); }
  const c = e.target.closest("[data-copy]");
  if (c) { navigator.clipboard.writeText(c.dataset.copy); toast("Copied"); }
});

// ---------------- theme & layout
function applyTheme(t) { if (t) document.documentElement.dataset.theme = t; const dark = (t || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark"; $("themeIcon").setAttribute("href", dark ? "#i-sun" : "#i-moon"); try { if (codeTermS) codeTermS.term.options.theme = codeTermTheme(); } catch {} }                       // codeTermS is declared later; at start-up there is no terminal yet
applyTheme(store.get("theme", null));
$("themeBtn").onclick = (e) => { e.stopPropagation(); const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); const n = cur === "dark" ? "light" : "dark"; store.set("theme", n); applyTheme(n); };
function setCollapsed(c) { $("app").classList.toggle("collapsed", c); $("expand").style.display = c ? "" : "none"; store.set("collapsed", c); }
setCollapsed(store.get("collapsed", innerWidth < 820));
$("collapse").onclick = () => setCollapsed(true); $("expand").onclick = () => setCollapsed(false); $("scrim").onclick = () => setCollapsed(true);
$("engineCard").onclick = () => showView("manage");

// ---------------- views
let view = "chat";
function chatHost() {
  return view === "code" && $("codeChatDock") ? $("codeChatDock") : $("v-chat");
}
function syncChatDock() {
  const host = chatHost();
  if (!host) return;
  const scroll = $("chatScroll"), jump = $("jumpBtn"), wrap = $("composerWrap");
  if (scroll && scroll.parentElement !== host) host.appendChild(scroll);
  if (jump && jump.parentElement !== host) host.appendChild(jump);
  const w = document.querySelector(".welcome");
  if (w && w.parentElement !== host) host.prepend(w);
  if (wrap && (!w || !w.contains(wrap)) && wrap.parentElement !== host) host.appendChild(wrap);
}
function showView(v) {
  view = v;
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("on", s.id === "v-" + v));
  document.querySelectorAll(".navitem[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  updateHeader();
  $("modelBtn").querySelector("svg").style.display = (v === "chat" || v === "code") ? "" : "none";
  closeMenus();
  if (v === "code" || v === "chat") {
    syncChatDock();
    renderThread();
    scrollBottom(true);                                                                 // the thread moved between views: show the latest turn
  }
  if (v === "code") {
    loadCodeView(); setTimeout(() => $("input").focus(), 0);
    codeTermStart().then(() => { if (codeTermS) codeTermS.term.options.theme = codeTermTheme(); });
  }
  if (v === "images") { loadGallery(); loadLibrary(); }
  if (v === "library") loadLibrary();
  if (v === "manage") loadManage();
  if (v === "search") { loadSearch(); setTimeout(() => $("srQuery").focus(), 0); }
  if (v === "agent") agOpen();
  if (v === "chat") { setTimeout(() => $("input").focus(), 0); agFetchStatus(); }
  renderHistory();
  if (innerWidth < 820) setCollapsed(true);
}
document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
function closeMenus() { $("modelMenu").classList.remove("on"); }
$("modelBtn").onclick = (e) => { e.stopPropagation(); if (view !== "chat" && view !== "code") return; $("modelMenu").classList.toggle("on"); };
document.addEventListener("click", (e) => { if (!e.target.closest("#modelMenu") && !e.target.closest("#modelBtn")) closeMenus(); if (!e.target.closest("#inspector") && !e.target.closest("#inspBtn")) $("inspector").classList.remove("on"); });
$("inspBtn").onclick = (e) => { e.stopPropagation(); $("inspector").classList.toggle("on"); };
$("inspClose").onclick = () => $("inspector").classList.remove("on");
$("mmSettings").onclick = () => { closeMenus(); $("settingsDlg").showModal(); };
$("settingsDone").onclick = () => { $("settingsDlg").close(); store.set("settings", { sys: $("sysPrompt").value, max: $("maxTok").value, temp: $("temp").value }); toast("Settings saved"); };
{ const s = store.get("settings", {}); $("sysPrompt").value = s.sys || ""; $("maxTok").value = s.max || DEFAULT_MAX_TOKENS; $("temp").value = s.temp || ""; }

// ---------------- engine status
let health = null, metrics = null, engineState = "down";
async function poll() {
  try { health = await (await fetch("/api/llm/health")).json(); } catch { health = null; }
  const st = health?.status;
  engineState = st === "ok" ? "ready" : (st === "loading" || st === "starting") ? "loading" : "down";
  metrics = null;
  if (engineState === "ready") { try { metrics = await (await fetch("/api/llm/metrics")).json(); } catch {} }
  const dotCls = engineState === "ready" ? "ok" : engineState === "loading" ? "warn" : "bad";
  const emode = health?.mode, vision = emode === "vision";
  const label = engineState === "ready" ? (vision ? "Ready · Vision" : emode === "gguf" ? "Ready · llama.cpp" : "Ready") : engineState === "loading" ? (health?.phase === "warming_up" ? "Warming up…" : "Loading model…") : "Engine stopped";
  runningName = engineState === "down" ? null : (health?.model_name || null);
  runningMode = engineState === "down" ? null : emode;
  updateHeader();
  $("engName").textContent = runningName || activeChat()?.name || "No model";
  $("mModel").textContent = runningName || activeChat()?.name || "–";
  $("engDesc").textContent = emode === "gguf" ? "llama.cpp server (GGUF)" : vision ? "mlx-vlm server (images + text)" : "mlx-dspark with speculative decoding";
  const port = emode === "gguf" ? 8082 : vision ? 8081 : 8080;
  $("apiUrl").textContent = `http://127.0.0.1:${port}/v1`;
  for (const id of ["engDot", "hdrDot"]) $(id).className = "dot " + dotCls;
  $("hdrStatusTxt").textContent = label;
  const mean = metrics?.mean_decode_tokens_per_sec;
  $("engSt").textContent = engineState === "ready" && mean ? `${label} · ${(+mean).toFixed(0)} tok/s avg` : label;
  $("mmBadge").textContent = label; $("mmBadge").className = "badge " + (engineState === "ready" ? "ok" : engineState === "loading" ? "warn" : "");
  $("mmEngineTxt").textContent = engineState === "down" ? "Start engine" : "Stop engine";
  $("engToggleTxt").textContent = engineState === "down" ? "Start" : "Stop";
  $("mStatus").textContent = label;
  if (health && engineState === "ready") {
    $("eDr").textContent = (health.drafter || "none").split("/").pop(); $("eMode").textContent = emode === "gguf" ? "llama.cpp" : (health.mode || "–");
    $("eCap").textContent = health.max_draft ?? "–"; $("eKv").textContent = health.kv_bits ? `${health.kv_bits}-bit` : "bf16";
    ctxWindow = health.context_window || ctxWindow; $("ctxMax").textContent = `${Math.round(ctxWindow / 1024)}k`;
    $("eWarn").textContent = (health.warnings || []).map((w) => w.message || w).join(" ");
  }
  if (metrics) {
    const m = metrics.memory || {}, s = metrics.system || {};
    $("eRes").textContent = m.active_bytes ? `${gib(m.active_bytes)} GB` : "–"; $("ePeak").textContent = m.peak_bytes ? `${gib(m.peak_bytes)} GB` : "–";
    $("ePress").textContent = s.pressure || "–"; $("eSwap").textContent = s.swap_used_bytes != null ? `${gib(s.swap_used_bytes)} GB` : "–";
    $("eReq").textContent = metrics.requests ?? "–"; $("eMean").textContent = mean ? `${(+mean).toFixed(1)} tok/s` : "–";
    $("mRes").textContent = m.active_bytes ? `${gib(m.active_bytes)} GB` : "–"; $("mSpeed").textContent = mean ? `${(+mean).toFixed(1)} tok/s` : "–"; $("mReq").textContent = metrics.requests ?? "–";
  } else { $("mRes").textContent = "–"; $("mSpeed").textContent = "–"; $("mReq").textContent = "–"; }
  try {
    const sys = await (await fetch("/api/system")).json();
    $("ramTxt").textContent = `${gib(sys.used_bytes)} / ${gib(sys.total_bytes)} GB`;
    $("ramBar").style.width = Math.min(100, sys.used_bytes / sys.total_bytes * 100) + "%";
  } catch {}
  updateSend();
}
setInterval(poll, 4000);
async function toggleEngine() {
  try {
    if (engineState === "down") { await jpost("/api/engine/start"); toast("Starting engine…"); }
    else { await jpost("/api/engine/stop"); toast("Engine stopped · memory released"); }
  } catch (e) { toast(e.message); }
  setTimeout(poll, 800);
}
$("mmEngine").onclick = () => { closeMenus(); toggleEngine(); };
$("engToggle").onclick = toggleEngine;

// ---------------- chats
let chats = [], current = null, streaming = null, ctxWindow = 32768;
let codeWorkspace = store.get("code_workspace", null);
let chRun = null;          // the task this chat started in Agent mode: { chat, msg, starting, s (run state, see agFresh), el, ui }
const chLive = () => !!(chRun && (chRun.starting || chRun.s.live));
let thinking = store.get("think", true), effort = store.get("effort", "");
async function loadChats() { try { chats = await api("/api/chats"); } catch { chats = []; } renderHistory(); }
function groupOf(ts) {
  const d = new Date(ts * 1000), now = new Date(), day = 864e5;
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (d >= start) return "Today"; if (d >= start - day) return "Yesterday"; if (d >= start - 7 * day) return "Previous 7 days"; if (d >= start - 30 * day) return "Previous 30 days"; return "Older";
}
function renderHistory() {
  const q = $("search").value.trim().toLowerCase();
  const list = chats.filter((c) => !q || c.title.toLowerCase().includes(q));
  const h = $("history");
  if (!list.length) { h.innerHTML = `<div class="hempty">${q ? "No matching chats" : "Your conversations will appear here."}</div>`; return; }
  let html = "", last = "";
  for (const c of list) {
    const g = groupOf(c.updated);
    if (g !== last) { html += `<div class="hgroup">${g}</div>`; last = g; }
    const on = (view === "chat" || view === "code") && current && current.id === c.id;
    const live = liveGen && liveGen.chat.id === c.id ? `<span class="hlive" title="Answering…"></span>` : "";
    html += `<div class="hitem${on ? " on" : ""}" data-id="${c.id}">${live}<span class="t">${esc(c.title)}</span><span class="acts"><button data-act="rename" title="Rename">${icon("i-pen")}</button><button data-act="delete" title="Delete">${icon("i-trash")}</button></span></div>`;
  }
  h.innerHTML = html;
}
$("search").oninput = renderHistory;
$("history").onclick = async (e) => {
  const item = e.target.closest(".hitem"); if (!item) return;
  const id = item.dataset.id, act = e.target.closest("button")?.dataset.act;
  if (act === "delete") {
    const c = chats.find((x) => x.id === id);
    if (!(await confirmBox("Delete chat?", `“${c?.title}” will be permanently deleted.`))) return;
    if (liveGen?.chat.id === id) { liveGen.chat.deleted = true; streaming?.abort(); }    // its reply must not save the chat again
    await api(`/api/chats/${id}`, { method: "DELETE" });
    if (current?.id === id) newChat();
    loadChats(); return;
  }
  if (act === "rename") {
    const t = item.querySelector(".t"), old = t.textContent;
    t.outerHTML = `<input value="${esc(old)}">`;
    const inp = item.querySelector("input"); inp.focus(); inp.select();
    let finished = false;
    const done = async (save) => { if (finished) return; finished = true; if (save && inp.value.trim() && inp.value !== old) { const c = await api(`/api/chats/${id}`); c.title = inp.value.trim(); await jpost(`/api/chats/${id}`, c, "PUT"); if (current?.id === id) current.title = c.title; if (liveGen?.chat.id === id) liveGen.chat.title = c.title; } loadChats(); };
    inp.onkeydown = (ev) => { if (ev.key === "Enter") done(true); if (ev.key === "Escape") done(false); };
    inp.onblur = () => done(true); return;
  }
  if (e.target.tagName === "INPUT") return;
  await openChat(id);
};
async function openChat(id) {
  let c;
  if (liveGen && liveGen.chat.id === id) c = liveGen.chat;                               // still answering: the copy in memory is the live one
  else { try { c = await api(`/api/chats/${id}`); } catch { return toast("Could not open chat"); } }
  chDetach(); current = c;
  if (c.workspace) { codeWorkspace = c.workspace; store.set("code_workspace", codeWorkspace); }
  if (view !== "code") showView("chat");
  else { syncChatDock(); renderThread(); loadCodeView(); }
  scrollBottom(true);
  chResume();
}
function newChat() {
  chDetach();
  const ws = view === "code" && activeCodeWs();
  current = ws ? { id: newId(), title: "New coding task", workspace: ws, messages: [] } : null;
  if (view !== "code") showView("chat");
  else syncChatDock();
  renderThread();
}
$("newChat").onclick = () => { chDetach(); current = null; showView("chat"); renderThread(); };
document.addEventListener("keydown", (e) => { if (e.metaKey && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); newChat(); } });
function updateWorkspaceUI() {
  const ws = current?.workspace;
  const pill = $("wsPill");
  if (!pill) return;
  const txt = $("wsTxt");
  const clear = $("wsClear");
  $("modeFoot").hidden = !ws; $("footTxt").hidden = !!ws;
  const compactBtn = $("compactBtn");
  if (compactBtn) compactBtn.style.display = (current?.messages?.length || 0) > 4 ? "" : "none";
  if (ws) {
    pill.classList.add("on");
    const name = ws.split("/").filter(Boolean).pop() || ws;
    if (txt) txt.textContent = name.length > 16 ? name.slice(0, 14) + "…" : name;
    pill.title = `Active workspace: ${ws} (click to change, or × to clear)`;
    if (clear) clear.style.display = "inline";
  } else {
    pill.classList.remove("on");
    if (txt) txt.textContent = "Workspace";
    pill.title = "Set active workspace folder for file & shell tools";
    if (clear) clear.style.display = "none";
  }
}
$("wsPill").onclick = async (e) => {
  if (e.target.id === "wsClear" || e.target.closest("#wsClear")) {
    if (current) {
      delete current.workspace;
      updateWorkspaceUI();
      await saveChat(current);
      toast("Workspace cleared");
    }
    return;
  }
  try {
    const r = await jpost("/api/search/choose-folder");
    if (r && r.path) {
      if (!current) current = { id: newId(), title: "New chat", messages: [] };
      current.workspace = r.path;
      codeWorkspace = r.path;
      store.set("code_workspace", r.path);
      updateWorkspaceUI();
      await saveChat(current);
      if (view === "code") loadCodeView();
      toast(`Workspace: ${r.path}`);
    }
  } catch (err) {
    toast(err.message || "Could not choose folder");
  }
};
async function saveChat(chat = current) {
  if (!chat) return;
  const msgs = chat.messages.map(({ role, content, reasoning, stats, thoughtSecs, error, attachments, finish, sources, agent_run, computerSetup, tool_steps, web_sources, plan, rounds, checkpoint }) => ({ role, content, reasoning, stats, thoughtSecs, error, attachments, finish, sources, agent_run, computerSetup, tool_steps, web_sources, plan, rounds, checkpoint }));
  await jpost(`/api/chats/${chat.id}`, { title: chat.title, messages: msgs, ...(chat.workspace ? { workspace: chat.workspace } : {}) }, "PUT");
  loadChats();
}

const CODE_STARTERS = [["i-book", "Explain this codebase", "Explain how this codebase is organized: the main modules, how they fit together, and where to start reading."], ["i-redo", "Fix failing tests", "Run the tests, find the first failure, and fix its root cause."], ["i-code", "Write tests for…", "Write tests for "], ["i-spark", "Add a feature…", "Add a feature that "]];
const STARTERS = [["i-code", "Write code", "Write a Python script that finds duplicate photos in a folder"], ["i-book", "Explain", "Explain how speculative decoding keeps output identical"], ["i-pencil2", "Draft", "Draft a polite follow-up email after a job interview"], ["i-spark", "Brainstorm", "Brainstorm names for a privacy-first note-taking app"]];
function renderThread() {
  updateWorkspaceUI();
  if (view === "code") codeUpdateStatus();
  const host = chatHost();
  const scroll = $("chatScroll"), wrap = $("composerWrap");
  let w = document.querySelector(".welcome");
  if (!current || !current.messages.length) {
    scroll.style.display = "none";
    if (!w) { w = document.createElement("div"); w.className = "welcome"; }
    if (w.parentElement !== host) host.prepend(w);
    const noModel = lib && !lib.chat.some((m) => m.installed);
    if (noModel) { renderOnboarding(w); return; }
    w.onclick = null; wrap.style.display = "";
    const hr = new Date().getHours(), code = view === "code";
    const heading = code ? "What should we build?" : hr < 5 ? "Working late?" : hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
    if (wrap.parentElement === w && w.querySelector(".starters") && w.querySelector("h1")?.textContent === heading) { $("input").placeholder = view === "code" ? "Describe a change, or type /" : `Ask ${activeChat()?.name || "anything"}`; renderHistory(); return; }
    const hadFocus = document.activeElement === $("input");
    w.innerHTML = `<h1>${code ? "" : `<span class="wmark">${icon("i-logo")}</span>`}${heading}</h1>`;
    if (code) w.insertAdjacentHTML("beforeend", `<p class="lead">Works in <b>${esc((activeCodeWs() || "").split("/").filter(Boolean).pop() || "your workspace")}</b>. Edits and commands ask before they run.</p>`);
    w.appendChild(wrap);
    if (hadFocus) $("input").focus();
    const st = document.createElement("div"); st.className = "starters";
    st.innerHTML = (code ? CODE_STARTERS : STARTERS).map(([ic, label, q]) => `<button data-q="${esc(q)}">${icon(ic)}${label}</button>`).join("");
    w.appendChild(st);
    st.onclick = (e) => {                                                                // code starters that end mid-sentence are filled in for the user to finish
      const b = e.target.closest("button"); if (!b) return;
      $("input").value = b.dataset.q; autosize();
      if (b.dataset.q.endsWith(" ")) { $("input").focus(); $("input").selectionStart = $("input").value.length; } else send();
    };
    $("input").placeholder = view === "code" ? "Describe a change, or type /" : `Ask ${activeChat()?.name || "anything"}`;
    showPerf(null); drawSpark();
  } else {
    if (w) { host.appendChild(wrap); w.remove(); }
    else if (wrap.parentElement !== host) host.appendChild(wrap);
    wrap.style.display = "";
    scroll.style.display = "";
    const keep = chRun && chRun.chat === current ? chRun.el : null;                     // the live card is the same element afterwards: typed answers and open panels stay
    $("thread").innerHTML = current.messages.map((m, i) => turnHTML(m, i)).join("");
    if (chRun && chRun.chat === current) {
      const ph = $("thread").querySelector(".agchat")?.closest(".turn");
      if (ph && keep) { keep.dataset.i = ph.dataset.i; ph.replaceWith(keep); } else if (ph) chBind(chRun, ph);
    }
    if (liveGen && liveGen.chat === current) liveGen.attach();
    $("input").placeholder = `Message ${activeChat()?.name || ""}`;
    const last = [...current.messages].reverse().find((m) => m.stats);
    showPerf(last?.stats); drawSpark();
  }
  renderHistory(); updateSend(); updateCtx();
}
// First run: no chat model yet — recommend one that fits this Mac's memory.
let sysRam = 0;
function renderOnboarding(w) {
  $("chatScroll").style.display = "none";
  const wrap = $("composerWrap"); chatHost().appendChild(wrap); wrap.style.display = "none";
  const gb = sysRam / 2 ** 30;
  const picks = gb >= 40 ? ["qwen3.8-27b", "gemma-4-12b", "lfm2.5-1.2b"] : gb >= 24 ? ["gemma-4-12b", "qwen3-8b", "lfm2.5-1.2b"]
    : gb >= 16 ? ["gguf-qwen3.5-9b", "qwen3-8b", "lfm2.5-1.2b"] : ["lfm2.5-1.2b", "gguf-qwen3.5-9b"];
  const models = picks.map((id) => lib.chat.find((m) => m.id === id)).filter(Boolean);
  const grid = w.querySelector("#obCards");
  if (grid) { patchCards(grid, models.map((m, i) => cardHTML({ ...m, blurb: (i === 0 ? "Recommended for this Mac. " : "") + m.blurb })).join(""), models); return; }
  w.innerHTML = `<div style="max-width:880px;width:100%">
    <h1 style="margin-bottom:8px">Choose your first model</h1>
    <p style="text-align:center;color:var(--muted);margin:0 0 26px">Models run entirely on this Mac. Picked for your ${Math.round(gb)} GB of memory — you can add more any time in the Model library.</p>
    <div class="cards" id="obCards">${models.map((m, i) => cardHTML({ ...m, blurb: (i === 0 ? "Recommended for this Mac. " : "") + m.blurb })).join("")}</div>
    <p style="text-align:center;margin-top:18px"><button class="btn sm" data-view="library" onclick="showView('library')">Browse all models</button></p></div>`;
  w.onclick = async (e) => {
    const b = e.target.closest("button[data-dl],button[data-cancel]"); if (!b) return;
    try { if (b.dataset.dl) { await jpost("/api/models/download", { id: b.dataset.dl }); toast("Downloading — chat unlocks when it finishes"); } else { await jpost("/api/models/cancel", { id: b.dataset.cancel }); } } catch (err) { toast(err.message); }
    loadLibrary();
  };

}
function perfLine(x) {
  if (!x) return "";
  const up = x.ceiling_tokens_per_sec ? ` · <b>${(x.decode_tokens_per_sec / x.ceiling_tokens_per_sec).toFixed(1)}×</b> vs plain` : "";
  const eng = x.mode === "vision" ? " · vision engine" : x.mode === "gguf" ? " · llama.cpp" : "";
  return `<button class="perf" data-perf title="Show details"><span><b>${x.decode_tokens_per_sec}</b> tok/s${up} · ${x.completion_tokens ?? "–"} tokens · ${x.ttft_seconds}s to first token${eng}</span></button>`;
}
function perfGrid(x) {
  if (!x) return "";
  const cells = [["Engine", x.mode === "vision" ? "Vision (mlx-vlm)" : x.mode === "gguf" ? "llama.cpp" : `MLX (${x.mode || "dspark"})`], ["Model", x.model || activeChat()?.name || "–"], ["Decode", `${x.decode_tokens_per_sec} tok/s`], ["End-to-end", `${x.tokens_per_sec} tok/s`], ["Speed-up vs plain", x.ceiling_tokens_per_sec ? `${(x.decode_tokens_per_sec / x.ceiling_tokens_per_sec).toFixed(2)}×` : "–"],
    ["Accept length", x.accept_len ?? "–"], ["First token", `${x.ttft_seconds}s`], ["Prefill", x.prefill_tokens_per_sec ? `${x.prefill_tokens_per_sec} tok/s` : "–"],
    ["Prompt tokens", x.prompt_tokens ?? "–"], ["Cached tokens", x.cached_tokens ?? "–"], ["Output tokens", x.completion_tokens ?? "–"], ["Decode time", x.decode_seconds != null ? `${x.decode_seconds}s` : "–"], ["Target passes", x.target_forwards ?? "–"], ["Draft cap", x.cap ?? "–"]];
  return `<div class="perfgrid">${cells.map(([l, v]) => `<div><div class="l">${l}</div><div class="v">${esc(v)}</div></div>`).join("")}</div>`;
}
function thoughtHTML(m, live) {
  if (!m.reasoning) return "";
  const thinkingNow = live && !m.content;
  const words = m.reasoning.trim().split(/\s+/).length;
  const label = thinkingNow ? `<span class="shimmer">Thinking…</span>` : `${m.thoughtSecs ? `Thought for ${fmtSecs(m.thoughtSecs)}` : "Thought process"} <span class="tw">· ${words.toLocaleString()} words</span>`;
  return `<details class="thought"${thinkingNow || m.thoughtOpen ? " open" : ""}><summary>${label}${icon("i-right")}</summary><div class="tb">${esc(m.reasoning)}</div></details>`;
}
function sourcesHTML(m) {
  if (!m.sources?.length) return "";
  const seen = new Set();                                    // one chip per file, however many passages it gave
  return `<div class="sources">${m.sources.filter((s) => !seen.has(s.path) && seen.add(s.path)).map((s) => `<button class="chip" data-open="${s.file_id}" data-path="${esc(s.path)}" title="${esc(srHome(s.path))}">${icon("i-file", "width:14px;height:14px")}${esc(s.name)}</button>`).join("")}</div>`;
}
const TOOL_LABELS = { read_file: "Read", write_file: "Write", edit_file: "Update", multi_edit_file: "Update", run_shell: "Bash", run_python: "Python",
  list_directory: "List", find_files: "Find", grep_files: "Search", code_outline: "Outline", git_status: "Git", git_diff: "Git diff", search_files: "My files",
  web_search: "Web search", read_webpage: "Fetch", todo_write: "Update todos", memory_store: "Remember", memory_recall: "Recall", delegate_task: "Task",
  manage_process: "Process", ask_user: "Ask", use_computer: "Computer", get_current_datetime: "Clock", reply: "Reply" };
const EDIT_TOOLS = ["write_file", "edit_file", "multi_edit_file"];
function toolLabel(name) {
  if (String(name || "").startsWith("mcp__")) { const [, srv, t] = name.split("__"); return `${srv} · ${t || "tool"}`; }
  return TOOL_LABELS[name] || name || "";
}
function diffLinesHTML(text, colored) {                                                  // one block line per text line; +/- coloured only for real diffs
  return String(text || "").split("\n").map((l) => {
    const c = !colored ? "" : l.startsWith("@@") ? " h" : l.startsWith("+") && !l.startsWith("+++") ? " a" : l.startsWith("-") && !l.startsWith("---") ? " d" : "";
    return `<span class="l${c}">${esc(l)}</span>`;
  }).join("");
}
function toolResultHTML(step) {
  if (step.status === "running") return `<span class="shimmer">Running…</span>`;
  if (step.error) return `<span class="tbad">${esc(String(step.error).split("\n")[0].slice(0, 180))}</span>`;
  const d = String(step.detail || ""), lines = d ? d.split("\n") : [];
  if (EDIT_TOOLS.includes(step.name)) {
    const add = lines.filter((l) => l.startsWith("+") && !l.startsWith("+++")).length, del = lines.filter((l) => l.startsWith("-") && !l.startsWith("---")).length;
    const checked = /\.(py|json|js|mjs|cjs|swift)\b/i.test(String(step.summary || ""));   // other types report "OK" without any checker
    const syn = step.syntax_ok === false ? `<span class="tbad">syntax error</span>` : step.syntax_check === "OK" && checked ? "syntax OK" : "";
    return [add || del ? `<span class="tadd">+${add}</span> <span class="tdel">−${del}</span>` : "no changes", syn].filter(Boolean).join(" · ");
  }
  if (step.name === "run_shell" || step.name === "run_python") {
    const rc = d.match(/\(exit code (-?\d+)\)\s*$/)?.[1];
    const out = lines.filter((l) => l.trim() && !/^(--- std(out|err) ---|\$ |\(exit code|\[Background)/.test(l));
    const last = out[out.length - 1] || (rc === "0" ? "Done" : "");
    return (rc && rc !== "0" ? `<span class="tbad">exit ${esc(rc)}</span>${last ? " · " : ""}` : "") + esc(last.slice(0, 160));
  }
  if (!lines.length) return "";
  if (step.name === "read_file") return `${lines.length} lines shown`;
  return esc(lines[0].slice(0, 160)) + (lines.length > 1 ? ` <span class="tmore">+${lines.length - 1} lines</span>` : "");
}
function chToolStepsInner(steps, openIds) {
  return (steps || []).map((step) => {
    const label = toolLabel(step.name), summary = String(step.summary || "Checking a tool");
    const ci = summary.indexOf(": "), arg = label ? (ci > 0 && ci < 40 ? summary.slice(ci + 2) : summary) : "";
    const st = step.status === "running" ? "running" : step.status === "error" || step.error ? "error" : "done";
    const undo = step.undone ? `<span class="undo-done">Restored</span>` : step.backup_id ? `<button class="undo-btn" data-undo="${esc(step.backup_id)}" title="Revert this change">Undo</button>` : "";
    const head = `<div class="th" title="${esc(step.error || summary)}"><span class="tdot"></span><span class="tname">${esc(label || summary)}</span>${arg ? `<span class="targ">${esc(arg)}</span>` : ""}${undo}${step.detail ? icon("i-right", "").replace('class="i"', 'class="i tchev"') : ""}</div><div class="tres">${toolResultHTML(step)}</div>`;
    if (!step.detail) return `<div class="chattoolstep st-${st}">${head}</div>`;
    const colored = EDIT_TOOLS.includes(step.name) || step.name === "git_diff";
    return `<details class="chattoolstep st-${st}" data-step-id="${esc(step.id || "")}"${openIds?.has(step.id) ? " open" : ""}><summary>${head}</summary><div class="chtdetail">${diffLinesHTML(step.detail, colored)}</div></details>`;
  }).join("");
}
function chToolsHTML(message) {
  const steps = Array.isArray(message.tool_steps) ? message.tool_steps : [];
  return steps.length ? `<div class="chattoolsteps">${chToolStepsInner(steps)}</div>` : "";
}
function chRoundItemHTML(r) {
  const th = thoughtHTML(r, false);
  const note = r.note ? `<div class="chround-note">${md(r.note)}</div>` : "";
  const tools = Array.isArray(r.tools) && r.tools.length ? `<div class="chattoolsteps">${chToolStepsInner(r.tools)}</div>` : "";
  return th || note || tools ? `<div class="chround">${th}${note}${tools}</div>` : "";
}
function chRoundsHTML(message) {
  return Array.isArray(message.rounds) && message.rounds.length ? `<div class="chroundswrap">${message.rounds.map(chRoundItemHTML).join("")}</div>` : "";
}
function chPlanHTML(message) {
  const plan = Array.isArray(message.plan) ? message.plan : [];
  if (!plan.length) return "";
  const done = plan.filter((t) => t.status === "completed").length;
  return `<div class="chplan${done === plan.length ? " st-done" : " st-running"}"><div class="chplan-hdr"><span class="tdot"></span><b>Todos</b><span class="sub">${done} of ${plan.length} done</span></div><ul class="chplan-list">${plan.map((t) => {
    const st = t.status || "pending";
    return `<li class="chplan-item ${esc(st)}"><span class="chplan-ico">${st === "completed" ? "✓" : ""}</span><span class="chplan-txt">${esc(t.content || t.task || "")}</span></li>`;
  }).join("")}</ul></div>`;
}
const PERM_WORDS = { edit: "file edits", exec: "commands", mcp: "this tool" };
function chHarnessCardHTML(message) {
  if (message.pendingApproval) {
    const a = message.pendingApproval, sid = esc(a.session_id);
    const tag = a.kind === "diff" ? "Edit" : a.kind === "shell" ? "Command" : a.kind === "code" ? "Run code" : "Tool";
    const scope = PERM_WORDS[a.perm_scope];
    return `<div class="chhcard approval" data-sid="${sid}">
      <div class="chhcard-hdr"><span class="chhcard-tag">${tag}</span><span class="chhcard-title">${esc(a.title || a.name || "Approval required")}</span></div>
      ${a.reason ? `<div class="chhcard-reason">${esc(a.reason)}</div>` : ""}
      ${a.preview ? `<div class="chhcard-preview">${diffLinesHTML(a.preview, a.kind === "diff")}</div>` : ""}
      <div class="chhcard-q">${a.kind === "diff" ? "Make this edit?" : "Do you want to proceed?"}</div>
      <div class="chhopts">
        <button class="chhopt on" data-hdecide="allow" data-sid="${sid}"><kbd>1</kbd>Yes</button>
        ${scope ? `<button class="chhopt" data-hdecide="allow_session" data-sid="${sid}"><kbd>2</kbd>Yes, and don’t ask again for ${scope} in this chat</button>` : ""}
        <button class="chhopt" data-hdeny-fb data-sid="${sid}"><kbd>${scope ? 3 : 2}</kbd>No, and tell the agent what to do instead</button>
      </div>
      <div class="chhcard-input-row" hidden><input type="text" class="chhcard-inp" data-fb placeholder="What should it do instead? (Enter sends, empty just declines)"><button class="btn sm" data-hdecide="deny" data-sid="${sid}">Decline</button></div>
      <div class="chhcard-hint">Press a number to choose · Esc declines</div>
    </div>`;
  }
  if (message.pendingQuestion) {
    const q = message.pendingQuestion, sid = esc(q.session_id);
    const opts = Array.isArray(q.options) ? q.options : [];
    return `<div class="chhcard question" data-sid="${sid}">
      <div class="chhcard-hdr"><span class="chhcard-tag">Question</span><span class="chhcard-title">${esc(q.question || "")}</span></div>
      ${opts.length ? `<div class="chhopts">${opts.map((opt, i) => `<button class="chhopt${i ? "" : " on"}" data-hopt="${esc(opt)}" data-sid="${sid}"><kbd>${i + 1}</kbd>${esc(opt)}</button>`).join("")}</div>` : ""}
      <div class="chhcard-input-row">
        <input type="text" class="chhcard-inp" placeholder="${opts.length ? "Or type another answer…" : "Type an answer…"}" data-sid="${sid}">
        <button class="btn sm primary" data-hqsend data-sid="${sid}">Send</button>
        <button class="btn sm" data-hqskip data-sid="${sid}">Skip</button>
      </div>
    </div>`;
  }
  return "";
}
function chWebSourcesHTML(message) {
  const seen = new Set();
  const sources = (Array.isArray(message.web_sources) ? message.web_sources : []).filter((source) => agWeb(source.url) && !seen.has(source.url) && seen.add(source.url));
  return sources.length ? `<div class="sources">${sources.map((source) => `<a class="chip" href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.title || source.url)}</a>`).join("")}</div>` : "";
}

function turnHTML(m, i, live = false) {
  if (m.role === "user") {
    const atts = m.attachments || [];
    const imgs = atts.filter((a) => a.kind === "image" && !a.scanned).map((a) => `<img src="${attThumb(a)}" alt="${esc(a.name)}" data-full="${attThumb(a)}">`).join("");
    const docs = atts.filter((a) => a.kind !== "image" || a.scanned).map((a) => `<div class="att"><div class="ic">${a.scanned ? `<img src="${attThumb(a)}" alt="">` : icon(DOC_ICON(a.name))}</div><div style="min-width:0"><div class="nm">${esc(a.name)}</div><div class="mt">${esc(attMeta(a))}</div></div></div>`).join("");
    return `<div class="turn user">${imgs || docs ? `<div class="uatts">${imgs}${docs}</div>` : ""}${m.content ? `<div class="bubble">${esc(m.content)}</div>` : ""}</div>`;
  }
  if (chRun && chRun.msg === m) return chCardHTML(i);
  if (liveGen && liveGen.bot === m) return liveSkeletonHTML(i);
  const hasRounds = Array.isArray(m.rounds) && m.rounds.length > 0;
  const waiting = live && !m.content && !m.reasoning && !hasRounds && !(m.tool_steps?.length);
  const agentRid = typeof m.agent_run === "string" && AG_RID.test(m.agent_run) ? m.agent_run : "";
  const idle = m.agent_run && !m.content && !m.error;                                  // saved while the task was still running
  const body = idle ? `<span class="agidle">This task has no saved result yet.</span>` : m.switching ? `<span class="shimmer">${m.switching === "vision" ? `Loading ${esc(m.switchModel || "the vision engine")} with vision… (about 10 s)` : `Loading ${esc(m.switchModel || "the model")}… (first load takes 10–30 s)`}</span>` : waiting ? `<span class="shimmer">${(current?.messages || []).some((x) => x.attachments?.length) ? "Reading attachments…" : "Thinking…"}</span>` : md(m.agent_run ? agNoImages(m.content) : m.content);
  const tail = live && m.content ? '<span class="caret"></span>' : "";
  const ckptHTML = !live && m.checkpoint?.turn_id
    ? (m.checkpoint.undone
        ? `<span class="undo-done">Turn reverted</span>`
        : `<button class="linkbtn undo-btn" data-undo-turn="${esc(m.checkpoint.turn_id)}" title="Revert all ${m.checkpoint.files?.length || 1} file edit(s) from this turn">Revert turn (${m.checkpoint.count || m.checkpoint.files?.length || 1})</button>`)
    : "";
  const actions = live ? "" : `<div class="actions"><button class="iconbtn" data-act="copy" title="Copy">${icon("i-copy")}</button>${m.agent_run ? "" : `<button class="iconbtn" data-act="regen" title="Regenerate">${icon("i-redo")}</button>`}${ckptHTML}${perfLine(m.stats)}</div>${perfGrid(m.stats)}`;
  const agentTail = (agentRid ? `<div class="agtask"><button class="linkbtn" data-agview="${esc(agentRid)}">Task · view steps</button></div>` : "")
    + (m.computerSetup ? `<div class="agtask"><button class="btn sm" data-computer-setup="${m.computerSetup === "enable" ? "enable" : "settings"}">${m.computerSetup === "enable" ? "Enable computer access and continue" : "Computer access settings"}</button></div>` : "");
  const cut = !live && m.finish === "length" ? `<div class="cutoff">${icon("i-x", "width:14px;height:14px")}The reply hit the length limit. <button class="btn sm" data-act="continue">Continue</button></div>` : "";
  const stepsBlock = hasRounds
    ? `${chPlanHTML(m)}${chRoundsHTML(m)}${thoughtHTML(m, live)}`
    : `${thoughtHTML(m, live)}${chPlanHTML(m)}${chToolsHTML(m)}`;
  return `<div class="turn bot" data-i="${i}">${stepsBlock}${chHarnessCardHTML(m)}<div class="content">${body}${tail}</div>${agentTail}${live ? "" : sourcesHTML(m) + chWebSourcesHTML(m)}${m.error ? `<div class="err">${esc(m.error)}</div>` : ""}${cut}${actions}</div>`;
}
$("thread").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.classList?.contains("chhcard-inp")) {
    e.preventDefault();
    e.target.closest(".chhcard")?.querySelector("[data-hqsend], [data-hdecide='deny']")?.click();
  } else if (e.key === "Escape" && e.target.matches?.("[data-fb]")) {
    e.preventDefault();
    e.target.closest(".chhcard")?.querySelector("[data-hdecide='deny']")?.click();
  } else {
    agLogKey(e);
  }
});
$("thread").onclick = async (e) => {
  const full = e.target.closest(".uatts img[data-full]");
  if (full) { window.open(full.dataset.full, "_blank"); return; }
  const src = e.target.closest(".sources [data-open]");
  if (src) { srOpen(src); return; }
  const task = e.target.closest("[data-agview]");
  if (task) { const rid = task.dataset.agview; if (AG_RID.test(rid)) chShowSteps(task, rid); return; }
  const setup = e.target.closest("[data-computer-setup]");
  if (setup) {
    if (setup.dataset.computerSetup !== "enable") { showView("manage"); $("agSet").scrollIntoView({ block: "center" }); return; }
    const chat = current, i = +setup.closest(".turn.bot").dataset.i, message = chat?.messages[i];
    setup.disabled = true;
    jpost("/api/agent/enable", { on: true }).then(() => {
      if (current === chat && chat.messages[i] === message && message?.computerSetup && !streaming && !chLive()) {
        chat.messages.splice(i, 1); renderThread(); generate();
      }
      loadAgent();
    }).catch((err) => { setup.disabled = false; toast(err.message); });
    return;
  }
  const undoTurnBtn = e.target.closest("[data-undo-turn]");
  if (undoTurnBtn) {
    e.preventDefault();
    e.stopPropagation();
    const tid = undoTurnBtn.dataset.undoTurn;
    const turnEl = undoTurnBtn.closest(".turn.bot");
    const turnMsg = turnEl ? current?.messages?.[+turnEl.dataset.i] : null;
    undoTurnBtn.disabled = true;
    const oldTxt = undoTurnBtn.textContent;
    undoTurnBtn.textContent = "Reverting turn…";
    try {
      const res = await jpost("/api/agent/undo-turn", { turn_id: tid });
      toast(`Reverted ${res.restored_count ?? res.count ?? 0} edit(s)`);
      if (turnMsg?.checkpoint) turnMsg.checkpoint.undone = true;
      for (const s of turnMsg?.tool_steps || []) if (s.backup_id) { s.undone = true; delete s.backup_id; }
      for (const r of turnMsg?.rounds || []) for (const s of r.tools || []) if (s.backup_id) { s.undone = true; delete s.backup_id; }
      if (turnEl) {
        turnEl.querySelectorAll("[data-undo]").forEach((b) => {
          const sp = document.createElement("span"); sp.className = "undo-done"; sp.textContent = "Restored"; b.replaceWith(sp);
        });
      }
      const doneSpan = document.createElement("span");
      doneSpan.className = "undo-done";
      doneSpan.textContent = "Turn reverted";
      undoTurnBtn.replaceWith(doneSpan);
      if (current && !streaming) saveChat(current);
      if (view === "code") loadCodeView(true);
    } catch (err) {
      toast(err.message || "Failed to revert turn");
      undoTurnBtn.disabled = false;
      undoTurnBtn.textContent = oldTxt;
    }
    return;
  }
  const undoBtn = e.target.closest("[data-undo]");
  if (undoBtn) {
    e.preventDefault();
    e.stopPropagation();
    const bid = undoBtn.dataset.undo;
    const turnEl = undoBtn.closest(".turn.bot");
    const turnMsg = turnEl ? current?.messages?.[+turnEl.dataset.i] : null;
    undoBtn.disabled = true;
    const oldTxt = undoBtn.textContent;
    undoBtn.textContent = "Reverting…";
    try {
      const res = await jpost("/api/agent/undo", { backup_id: bid });
      toast(`Restored ${res.name || "file"}`);
      const step = (turnMsg?.tool_steps || []).find((s) => s.backup_id === bid);
      if (step) { step.undone = true; delete step.backup_id; }
      for (const r of turnMsg?.rounds || []) {
        const rs = (r.tools || []).find((s) => s.backup_id === bid);
        if (rs) { rs.undone = true; delete rs.backup_id; }
      }
      const doneSpan = document.createElement("span");
      doneSpan.className = "undo-done";
      doneSpan.textContent = "Restored";
      undoBtn.replaceWith(doneSpan);
      if (current && !streaming) saveChat(current);
      if (view === "code") loadCodeView(true);
    } catch (err) {
      toast(err.message || "Failed to restore");
      undoBtn.disabled = false;
      undoBtn.textContent = oldTxt;
    }
    return;
  }
  const fbBtn = e.target.closest("[data-hdeny-fb]");
  if (fbBtn) {
    const card = fbBtn.closest(".chhcard"), row = card?.querySelector(".chhcard-input-row");
    card?.querySelectorAll(".chhopt").forEach((b) => b.classList.toggle("on", b === fbBtn));
    if (row) { row.hidden = false; row.querySelector("input")?.focus(); }
    return;
  }
  const decideBtn = e.target.closest("[data-hdecide]");
  if (decideBtn) {
    const sid = decideBtn.dataset.sid, decision = decideBtn.dataset.hdecide;
    const card = decideBtn.closest(".chhcard"), turnEl = decideBtn.closest(".turn.bot");
    const turnMsg = turnEl ? current?.messages?.[+turnEl.dataset.i] : null;
    const feedback = decision === "deny" ? (card?.querySelector("[data-fb]")?.value || "").trim() : "";
    if (card) card.querySelectorAll("button, input").forEach((b) => b.disabled = true);
    try {
      await jpost(`/api/chat/sessions/${sid}/reply`, { decision, ...(feedback ? { feedback } : {}) });
      if (turnMsg) delete turnMsg.pendingApproval;
      if (card) card.remove();
    } catch (err) {
      toast(err.message || "Approval reply failed");
      if (card) card.querySelectorAll("button, input").forEach((b) => b.disabled = false);
    }
    return;
  }
  const optBtn = e.target.closest("[data-hopt]");
  if (optBtn) {
    const sid = optBtn.dataset.sid, answer = optBtn.dataset.hopt;
    const card = optBtn.closest(".chhcard"), turnEl = optBtn.closest(".turn.bot");
    const turnMsg = turnEl ? current?.messages?.[+turnEl.dataset.i] : null;
    if (card) card.querySelectorAll("button, input").forEach((b) => b.disabled = true);
    try {
      await jpost(`/api/chat/sessions/${sid}/reply`, { answer });
      if (turnMsg) delete turnMsg.pendingQuestion;
      if (card) card.remove();
    } catch (err) {
      toast(err.message || "Reply failed");
      if (card) card.querySelectorAll("button, input").forEach((b) => b.disabled = false);
    }
    return;
  }
  const qSendBtn = e.target.closest("[data-hqsend]");
  if (qSendBtn) {
    const card = qSendBtn.closest(".chhcard"), turnEl = qSendBtn.closest(".turn.bot");
    const turnMsg = turnEl ? current?.messages?.[+turnEl.dataset.i] : null;
    const inp = card?.querySelector(".chhcard-inp");
    const sid = qSendBtn.dataset.sid, answer = inp?.value?.trim() || "";
    if (card) card.querySelectorAll("button, input").forEach((b) => b.disabled = true);
    try {
      await jpost(`/api/chat/sessions/${sid}/reply`, { answer });
      if (turnMsg) delete turnMsg.pendingQuestion;
      if (card) card.remove();
    } catch (err) {
      toast(err.message || "Reply failed");
      if (card) card.querySelectorAll("button, input").forEach((b) => b.disabled = false);
    }
    return;
  }
  const qSkipBtn = e.target.closest("[data-hqskip]");
  if (qSkipBtn) {
    const card = qSkipBtn.closest(".chhcard"), turnEl = qSkipBtn.closest(".turn.bot");
    const turnMsg = turnEl ? current?.messages?.[+turnEl.dataset.i] : null;
    const sid = qSkipBtn.dataset.sid;
    if (card) card.querySelectorAll("button, input").forEach((b) => b.disabled = true);
    try {
      await jpost(`/api/chat/sessions/${sid}/reply`, { answer: "" });
      if (turnMsg) delete turnMsg.pendingQuestion;
      if (card) card.remove();
    } catch (err) {
      toast(err.message || "Reply failed");
      if (card) card.querySelectorAll("button, input").forEach((b) => b.disabled = false);
    }
    return;
  }
  const card = e.target.closest(".agchat");
  if (card) { if (chRun?.el?.contains(card)) chCardClick(chRun, e); return; }
  const turn = e.target.closest(".turn.bot"); if (!turn) return;
  if (e.target.closest("[data-perf]")) { turn.querySelector(".perfgrid")?.classList.toggle("on"); return; }
  const act = e.target.closest("[data-act]")?.dataset.act, m = current.messages[+turn.dataset.i];
  if (act === "copy") { navigator.clipboard.writeText(m.content); toast("Copied to clipboard"); }
  if (act === "continue" && !streaming) {
    noFilesOnce = true;                                    // "continue" is not a question to look up in the user's files
    $("input").value = "Continue exactly where you left off."; autosize(); send(); return;
  }
  if (act === "regen" && !streaming && !chLive() && !m.agent_run) {
    current.messages = current.messages.slice(0, +turn.dataset.i); renderThread(); generate();
  }
};
function showPerf(x) {
  $("tDec").innerHTML = `${x?.decode_tokens_per_sec ?? "–"}<small>tok/s</small>`;
  $("tUp").innerHTML = `${x?.ceiling_tokens_per_sec ? (x.decode_tokens_per_sec / x.ceiling_tokens_per_sec).toFixed(2) : "–"}<small>×</small>`;
  $("tAcc").textContent = x?.accept_len ?? "–"; $("tTtft").innerHTML = `${x?.ttft_seconds ?? "–"}<small>s</small>`;
  const ctx = x?.context_tokens || 0; $("ctxTxt").textContent = `${ctx.toLocaleString()} tokens`; $("ctxBar").style.width = Math.min(100, ctx / ctxWindow * 100) + "%";
}
function drawSpark() {
  const pts = (current?.messages || []).filter((m) => m.stats?.decode_tokens_per_sec).slice(-20).map((m) => m.stats.decode_tokens_per_sec);
  const svg = $("spark"); if (pts.length < 2) { svg.innerHTML = ""; return; }
  const max = Math.max(...pts) * 1.1, W = 280, H = 44;
  const d = pts.map((v, i) => `${i ? "L" : "M"}${(i / (pts.length - 1) * W).toFixed(1)} ${(H - v / max * (H - 4) - 2).toFixed(1)}`).join(" ");
  svg.innerHTML = `<path d="${d} L${W} ${H} L0 ${H}Z" fill="currentColor" opacity=".07"/><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
}
// Auto-scroll only while the reader is at the bottom; scrolling up pauses it (a "↓" button returns).
let stick = true, autoTop = -1;                                                             // autoTop: where scrollBottom last put the view
const fmtSecs = (x) => { const n = Math.max(1, Math.round(x || 0)); return n === 1 ? "1 second" : n < 90 ? `${n} seconds` : `${Math.round(n / 60)} minutes`; };
function scrollBottom(force) {
  const s = $("chatScroll");
  if (force) stick = true;
  if (stick) { s.scrollTop = s.scrollHeight; autoTop = s.scrollTop; }
  $("jumpBtn").classList.toggle("on", !stick && s.scrollHeight - s.scrollTop - s.clientHeight > 80);
}
$("chatScroll").addEventListener("scroll", () => {
  const s = $("chatScroll"), gap = s.scrollHeight - s.scrollTop - s.clientHeight;
  // Our own scroll lands here too, sometimes after the reply grew or the composer area changed height: only a scroll
  // that moved the view away from where scrollBottom put it is the user's, and only that may stop following.
  if (stick && Math.abs(s.scrollTop - autoTop) < 2) return;
  stick = gap < 40;
  $("jumpBtn").classList.toggle("on", gap > 80);
}, { passive: true });
$("chatScroll").addEventListener("wheel", (e) => { if (e.deltaY < 0) stick = false; }, { passive: true });
$("jumpBtn").onclick = () => scrollBottom(true);
function modelThinks() { try { const a = activeChat(); return !a || a.thinking !== false; } catch { return true; } }   // lib may not exist yet at start-up

// attachments
let pending = [];            // {key, name, busy, meta?, error?, preview?}
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
  $("atts").innerHTML = pending.map((p, i) => {
    const m = p.meta, isImg = (m && m.kind === "image" && !m.scanned) || (!m && p.preview);
    const thumb = m ? attThumb(m) : p.preview;
    const ic = thumb ? `<img src="${thumb}" alt="">` : icon(DOC_ICON(p.name));
    const sub = p.error ? `<div class="mt warn">${esc(p.error)}</div>` : p.busy ? `<div class="mt">Reading…</div>` : `<div class="mt${m.truncated ? " warn" : ""}">${esc(attMeta(m))}${m.truncated ? " · truncated" : ""}</div>`;
    return `<div class="att${isImg ? " img" : ""}${p.busy ? " busy" : ""}" title="${esc(p.name)}"><div class="ic">${ic}</div>${isImg ? "" : `<div style="min-width:0"><div class="nm">${esc(p.name)}</div>${sub}</div>`}<button class="x" data-rm="${i}" title="Remove">${icon("i-x")}</button></div>`;
  }).join("");
  updateSend();
}
$("atts").onclick = (e) => { const b = e.target.closest("[data-rm]"); if (b) { pending.splice(+b.dataset.rm, 1); renderPending(); } };
async function addFiles(files) {
  for (const f of files) {
    const p = { key: Math.random(), name: f.name || "pasted-image.png", busy: true, preview: f.type.startsWith("image/") ? URL.createObjectURL(f) : null };
    pending.push(p); renderPending();
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

// composer
const SLASH_CMDS = [
  { cmd: "/review", desc: "3-pass code review of uncommitted git changes (Bugs, Security, Compliance)" },
  { cmd: "/test", desc: "Run the repository's verification / test suite and fix failures" },
  { cmd: "/commit", desc: "Inspect git status & diff, draft a commit message, and commit" },
  { cmd: "/simplify", desc: "Refactor recently modified code for clarity and simplicity" },
  { cmd: "/init", desc: "Scan workspace and generate a tailored CLAUDE.md / AGENTS.md file" },
  { cmd: "/compact", desc: "Compact older conversation turns into a structured state summary" }
];
let acItems = [], acIndex = 0, acMode = null, acStart = 0, acTimer = 0;
function closeAc() {
  acItems = []; acIndex = 0; acMode = null;
  const m = $("acMenu"); if (m) { m.classList.remove("on"); m.innerHTML = ""; }
}
function renderAc() {
  const m = $("acMenu"); if (!m) return;
  if (!acItems.length) { closeAc(); return; }
  m.classList.add("on");
  m.innerHTML = acItems.map((it, i) => `<div class="acitem${i === acIndex ? " on" : ""}" data-aci="${i}"><span class="acmain">${esc(it.label)}</span><span class="acsub">${esc(it.sub || "")}</span></div>`).join("");
}
function applyAc(idx) {
  const it = acItems[idx]; if (!it) return;
  const inp = $("input"), val = inp.value || "", pos = inp.selectionStart ?? val.length;
  if (acMode === "slash") {
    inp.value = it.insert + " ";
    inp.selectionStart = inp.selectionEnd = inp.value.length;
  } else if (acMode === "mention") {
    const before = val.slice(0, acStart), after = val.slice(pos);
    const next = before + "@" + it.insert + (after.startsWith(" ") ? "" : " ") + after;
    inp.value = next;
    const newPos = (before + "@" + it.insert + " ").length;
    inp.selectionStart = inp.selectionEnd = newPos;
  }
  closeAc(); autosize(); inp.focus();
}
$("acMenu").onmousedown = (e) => {
  const row = e.target.closest("[data-aci]");
  if (row) { e.preventDefault(); applyAc(+row.dataset.aci); }
};
function updateAc() {
  const inp = $("input"), val = inp.value || "", pos = inp.selectionStart ?? val.length;
  const before = val.slice(0, pos);
  const slashM = before.match(/^\/([a-zA-Z]*)$/);
  if (slashM) {
    const q = slashM[1].toLowerCase();
    acMode = "slash"; acStart = 0; acIndex = 0;
    acItems = SLASH_CMDS.filter((c) => c.cmd.slice(1).startsWith(q)).map((c) => ({ label: c.cmd, sub: c.desc, insert: c.cmd }));
    renderAc();
    return;
  }
  const mentM = before.match(/(?:^|\s)@([A-Za-z0-9_./-]*)$/);
  if (mentM) {
    const q = mentM[1];
    acMode = "mention"; acStart = pos - q.length - 1;
    clearTimeout(acTimer);
    acTimer = setTimeout(async () => {
      try {
        const ws = current?.workspace ? `&workspace=${encodeURIComponent(current.workspace)}` : "";
        const r = await api(`/api/workspace/files?q=${encodeURIComponent(q)}&limit=12${ws}`);
        acIndex = 0;
        acItems = (r.items || []).map((f) => ({ label: "@" + f.path + (f.type === "dir" ? "/" : ""), sub: f.type === "dir" ? "folder" : "file", insert: f.path }));
        renderAc();
      } catch { closeAc(); }
    }, 60);
    return;
  }
  closeAc();
}
function autosize() { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(240, t.scrollHeight) + "px"; updateSend(); }
$("input").addEventListener("input", () => { autosize(); updateAc(); });
$("input").addEventListener("blur", () => setTimeout(closeAc, 150));
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Tab" && e.shiftKey && !e.isComposing && (current?.workspace || view === "code")) { e.preventDefault(); toggleMode(); return; }
  if (acItems.length && $("acMenu")?.classList.contains("on")) {
    if (e.key === "ArrowDown") { e.preventDefault(); acIndex = (acIndex + 1) % acItems.length; renderAc(); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); acIndex = (acIndex - 1 + acItems.length) % acItems.length; renderAc(); return; }
    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !e.isComposing)) { e.preventDefault(); applyAc(acIndex); return; }
    if (e.key === "Escape") { e.preventDefault(); closeAc(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
// How full the model's context is: the last reply's measured prompt + output tokens, plus an estimate (chars / 3.2, as
// app.py clamps) for anything added since. The agent's system note and tools cost about 2k tokens on a first turn.
function ctxUsage(chat = current) {
  const msgs = chat?.messages || [];
  if (!msgs.length) return null;
  let i = msgs.length - 1;
  while (i >= 0 && !msgs[i].stats?.prompt_tokens) i--;
  let used = i >= 0 ? (msgs[i].stats.prompt_tokens || 0) + (msgs[i].stats.completion_tokens || 0) : 2000;
  for (const m of msgs.slice(i + 1)) used += Math.ceil((m.content || "").length / 3.2) + (m.attachments || []).reduce((a, x) => a + (x.tokens_est || (x.kind === "image" ? 1200 : 0)), 0);
  return { used, total: ctxWindow, measured: i === msgs.length - 1 };
}
const fmtTok = (n) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
function updateCtx() {
  const u = ctxUsage(), f = $("ctxFoot");
  if (!u) { f.hidden = true; return; }
  const pct = Math.min(100, Math.round(u.used / u.total * 100)), free = Math.max(0, u.total - u.used);
  const level = pct >= 90 ? "bad" : pct >= 70 ? "warn" : "";
  const canCompact = (current?.messages?.length || 0) > 4;
  f.hidden = false;
  f.className = `ctxfoot${level ? " " + level : ""}${canCompact && pct >= 50 ? " click" : ""}`;
  f.style.setProperty("--ctxp", pct);
  f.innerHTML = `<span class="ctxring"></span>${u.measured ? "" : "~"}${pct}% context used · ${fmtTok(free)} of ${Math.round(u.total / 1024)}k free${level && canCompact ? " · compact" : ""}`;
  f.title = `${u.used.toLocaleString()} of ${u.total.toLocaleString()} tokens in this conversation${u.measured ? "" : " (estimated)"}.${canCompact ? " Click to compact older turns into a summary." : ""}`;
  if (view === "code") codeUpdateStatus();
}
let permMode = "ask", workTimer = 0, workStart = 0;                                    // "accept edits" lasts until the page reloads, never across launches
function syncMode() {
  const on = permMode === "accept_edits";
  $("modeFoot").classList.toggle("on", on);
  $("modeFoot").textContent = `${on ? "⏵⏵ Accept edits on" : "✎ Ask before edits"} · shift+tab to switch`;
  const cm = $("codeModeBtn");
  if (cm) { cm.classList.toggle("on", on); cm.textContent = on ? "⏵⏵ Accept edits" : "✎ Ask before edits"; }
}
function toggleMode() {
  permMode = permMode === "accept_edits" ? "ask" : "accept_edits";
  syncMode();
  toast(permMode === "accept_edits" ? "File edits run without asking (commands still ask)" : "Every file edit asks first");
}
$("modeFoot").onclick = toggleMode;
$("ctxFoot").onclick = () => { if ($("ctxFoot").classList.contains("click")) compactCurrentChat().then(updateCtx); };
$("workLine").onclick = (e) => { if (e.target.closest("[data-workopen]") && liveGen) openChat(liveGen.chat.id); };
function tickWork() {
  const secs = Math.round((Date.now() - workStart) / 1000), elsewhere = liveGen && liveGen.chat !== current;
  const waiting = liveGen ? !!(liveGen.bot.pendingApproval || liveGen.bot.pendingQuestion) : !!document.querySelector("#thread .chhcard");
  $("workTxt").textContent = elsewhere ? `${waiting ? "Waiting for you" : "Working"} in “${liveGen.chat.title}”` : waiting ? "Waiting for you…" : "Working…";
  $("workHint").innerHTML = elsewhere ? `· <button class="workopen" data-workopen>Open</button>` : `· <kbd>esc</kbd> to interrupt`;
  $("workTime").textContent = secs >= 1 ? `${secs < 60 ? secs + "s" : Math.floor(secs / 60) + "m " + (secs % 60) + "s"}` : "";
}
function updateSend() {
  const here = !liveGen || liveGen.chat === current;
  const b = $("send"), working = (!!streaming && here) || chLive();
  const wl = $("workLine"), anyWork = !!streaming || chLive();
  if (anyWork && wl.hidden) { wl.hidden = false; workStart = Date.now(); clearInterval(workTimer); workTimer = setInterval(tickWork, 1000); }
  else if (!anyWork && !wl.hidden) { wl.hidden = true; clearInterval(workTimer); }
  if (anyWork) tickWork();
  $("sendIcon").setAttribute("href", working ? "#i-stop" : "#i-up");
  b.title = working ? "Stop" : "Send";
  const ready = pending.filter((p) => p.meta).length, busy = pending.some((p) => p.busy);
  b.disabled = !working && (busy || (!$("input").value.trim() && !ready));
}
$("send").onclick = () => streaming && (!liveGen || liveGen.chat === current) ? streaming.abort() : chLive() ? chStop() : send();
syncMode();
document.addEventListener("keydown", (e) => {                                            // permission prompts answer to 1/2/3 and Esc, like Claude Code; Esc otherwise stops the reply
  if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing || e.defaultPrevented) return;
  if (document.querySelector("dialog[open]")) return;
  const t = e.target, editable = t.closest?.("input, textarea, select, [contenteditable]");
  const card = [...document.querySelectorAll("#thread .chhcard")].pop();
  if (card && !card.querySelector(".chhopt:disabled") && (!editable || (t.id === "input" && !t.value))) {
    const opts = [...card.querySelectorAll(".chhopt")];
    if (/^[1-9]$/.test(e.key) && opts[+e.key - 1]) { e.preventDefault(); opts[+e.key - 1].click(); return; }
    if (e.key === "Escape" && card.classList.contains("approval")) { e.preventDefault(); card.querySelector("[data-hdecide='deny']")?.click(); return; }
  }
  const stoppable = (streaming && (!liveGen || liveGen.chat === current)) || chLive();
  if (e.key === "Escape" && stoppable && (!editable || t.id === "input") && !$("acMenu").classList.contains("on")) { e.preventDefault(); $("send").click(); }
});
function syncThink() {
  const can = modelThinks();
  $("thinkPill").classList.toggle("on", thinking && can);
  $("thinkPill").disabled = !can; $("effortSel").disabled = !can;
  $("thinkPill").title = can ? "Reason step by step before answering" : `${activeChat()?.name || "This model"} doesn't have a thinking mode`;
  $("thinkSw").checked = thinking; store.set("think", thinking);
}
$("thinkPill").onclick = () => { thinking = !thinking; syncThink(); };
$("thinkSw").onchange = () => { thinking = $("thinkSw").checked; syncThink(); };
function syncEffort() { $("effortSel").value = effort; $("effortSeg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x.dataset.v === effort)); store.set("effort", effort); }
$("effortSel").onchange = () => { effort = $("effortSel").value; syncEffort(); };
$("effortSeg").onclick = (e) => { const b = e.target.closest("button"); if (b) { effort = b.dataset.v; syncEffort(); } };
syncThink(); syncEffort();

async function compactCurrentChat() {
  if (!current || (current.messages || []).length <= 4) { toast("Conversation is already compact"); return; }
  try {
    const r = await jpost("/api/chat/compact", { chat_id: current.id, messages: current.messages, keep_recent: 4 });
    if (r.compacted && Array.isArray(r.messages)) {
      current.messages = r.messages;
      renderThread();
      await saveChat(current);
      toast(`Compacted ${(r.before_count || 0) - (r.after_count || 0) + 1} earlier messages`);
    } else {
      toast("Conversation is already compact");
    }
  } catch (err) { toast(err.message || "Compaction failed"); }
}
$("compactBtn").onclick = compactCurrentChat;

async function send() {
  const text = $("input").value.trim();
  const atts = pending.filter((p) => p.meta).map((p) => p.meta);
  if ((text || atts.length) && streaming && liveGen && liveGen.chat !== current) return toast(`Still answering in “${liveGen.chat.title}”. Wait for it, or open it and stop it.`);
  if ((!text && !atts.length) || streaming || chLive() || pending.some((p) => p.busy)) return;
  closeAc();
  if (text === "/compact" || text.startsWith("/compact ")) {
    $("input").value = ""; autosize();
    await compactCurrentChat();
    return;
  }
  $("input").value = ""; pending = []; renderPending(); autosize();
  const title = (text || atts.map((a) => a.name).join(", ")).replace(/\s+/g, " ").slice(0, 48);
  if (!current) current = { id: newId(), title, messages: [] };
  else if (!current.messages.length) current.title = title;                            // a placeholder ("New coding task", "New chat") until the first message
  current.messages.push({ role: "user", content: text, attachments: atts.length ? atts : undefined });
  saveChat(current);                                                                    // in the sidebar now, so the user can leave and come back mid-reply
  renderThread(); scrollBottom(true);
  generate();
}
// The reply being generated. It outlives the view: the user may open other chats or views meanwhile, and whenever its
// chat is drawn again (renderThread) turnHTML puts an empty live skeleton in its place and the stream re-attaches to it.
let liveGen = null;                                      // { chat, bot, attach() }
const liveSkeletonHTML = (i, waitingText = "Thinking…") => `<div class="turn bot" data-i="${i}" data-live="1">
    <div class="chplanwrap"></div>
    <div class="chroundswrap"></div>
    <details class="thought" hidden><summary><span class="tl shimmer">Thinking…</span>${icon("i-right")}</summary><div class="tb"></div></details>
    <div class="chround-note" hidden></div>
    <div class="chattoolsteps"></div>
    <div class="chhcardwrap"></div>
    <div class="content"><span class="shimmer">${waitingText}</span></div>
    <div class="err" hidden></div></div>`;
async function generate() {
  const chat = current;
  const bot = { role: "assistant", content: "", reasoning: "" };
  current.messages.push(bot);
  const idx = current.messages.length - 1;
  // Stable DOM for the live reply: the thinking panel and answer are updated in place, so the
  // panel keeps its open/closed state and its own scroll position while tokens stream in.
  $("thread").insertAdjacentHTML("beforeend", liveSkeletonHTML(idx, current.messages.some((x) => x.attachments?.length) ? "Reading attachments…" : "Thinking…"));
  let el, planWrap, roundsWrap, th, tl, tb, noteEl, liveToolsEl, cardWrap, contentEl, errEl;
  let userToggled = false;                               // a click on the header means "leave it as I set it"
  const setOpen = (v) => { th.open = v; };
  let thinkStart = 0, thinkEnd = 0, raf = 0, pendingThought = "", actionRun = null, lastCardKey = "|";
  let curTools = [], curNote = "", curRoundNo = null, renderedRounds = 0;
  const thinkingNow = () => thinkStart && !thinkEnd;
  const bind = (node) => {                               // point the stream at a (new) live element and draw what it has so far
    el = node;
    planWrap = el.querySelector(".chplanwrap"); roundsWrap = el.querySelector(".chroundswrap");
    th = el.querySelector(":scope > .thought"); tl = th.querySelector(".tl"); tb = th.querySelector(".tb");
    noteEl = el.querySelector(":scope > .chround-note"); liveToolsEl = el.querySelector(":scope > .chattoolsteps");
    cardWrap = el.querySelector(".chhcardwrap"); contentEl = el.querySelector(".content"); errEl = el.querySelector(".err");
    th.querySelector("summary").addEventListener("click", () => { userToggled = true; });
    renderedRounds = 0; lastCardKey = "|"; pendingThought = "";
    if (bot.reasoning) {
      th.hidden = false; tb.textContent = bot.reasoning;
      if (thinkingNow()) { tl.className = "tl shimmer"; tl.textContent = "Thinking…"; th.open = !userToggled || th.open; }
      else { tl.className = "tl"; tl.innerHTML = thoughtLabel(); }
    }
  };
  bind($("thread").lastElementChild);
  const attach = () => {                                 // called by renderThread after it redrew this chat
    const node = current === chat && $("thread").querySelector(`.turn.bot[data-live="1"][data-i="${chat.messages.indexOf(bot)}"]`);
    if (node && node !== el) { bind(node); if (raf) { raf < 0 ? clearTimeout(-raf) : cancelAnimationFrame(raf); } paint(); }
  };
  liveGen = { chat, bot, attach };
  stick = true; scrollBottom(true);

  const s = store.get("settings", {});
  const messages = [];
  if (s.sys) messages.push({ role: "system", content: s.sys });
  current.messages.slice(0, -1).filter((m) => !(m.agent_run && !m.content)).forEach((m) => messages.push({ role: m.role, content: m.content, ...(m.attachments ? { attachments: m.attachments.map((a) => a.id) } : {}) }));
  const body = { messages, chat_id: chat.id, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, max_tokens: +s.max || DEFAULT_MAX_TOKENS, enable_thinking: thinking && modelThinks() };
  if (effort && modelThinks()) body.reasoning_effort = effort;
  if (s.temp) body.temperature = +s.temp;
  if (useFiles && sr?.running && !sr.error && !noFilesOnce) body.use_files = true;
  if (current?.workspace) body.workspace = current.workspace;
  if (current?.workspace && permMode === "accept_edits") body.permission_mode = "accept_edits";
  noFilesOnce = false;
  streaming = new AbortController(); updateSend(); renderHistory();

  const thoughtLabel = () => {
    const words = (bot.reasoning || "").trim().split(/\s+/).filter(Boolean).length;
    return `${bot.thoughtSecs ? `Thought for ${fmtSecs(bot.thoughtSecs)}` : "Thought process"}${words ? ` <span class="tw">· ${words.toLocaleString()} words</span>` : ""}`;
  };
  const finishRoundThinking = () => {
    if (thinkStart && !thinkEnd) {
      thinkEnd = performance.now();
      bot.thoughtSecs = (thinkEnd - thinkStart) / 1000;
      tl.className = "tl";
      tl.innerHTML = thoughtLabel();
      if (!userToggled) setOpen(false);                  // collapse once the round's action/answer starts, unless the user opened it
    }
  };
  const advanceRound = () => {
    if (!curTools.length) return;
    if (pendingThought) { tb.append(pendingThought); pendingThought = ""; }
    finishRoundThinking();
    const noteTxt = curNote || (bot.content || "").trim();
    bot.rounds ||= [];
    bot.rounds.push({
      reasoning: bot.reasoning || "",
      thoughtSecs: bot.thoughtSecs || 0,
      thoughtOpen: th.open,
      note: noteTxt,
      tools: curTools
    });
    curTools = [];
    curNote = "";
    curRoundNo = null;
    bot.reasoning = "";
    bot.thoughtSecs = 0;
    bot.content = "";
    thinkStart = 0;
    thinkEnd = 0;
    userToggled = false;
    tb.textContent = "";
    th.hidden = true;
    th.open = false;
    tl.className = "tl shimmer";
    tl.textContent = "Thinking…";
    if (noteEl) { noteEl.hidden = true; noteEl.innerHTML = ""; }
  };

  const paint = () => {
    raf = 0;
    if (!el.isConnected) return;                        // the user is looking at another chat or view; the reply keeps arriving in `bot`
    if (pendingThought) {
      const atEnd = tb.scrollHeight - tb.scrollTop - tb.clientHeight < 24;
      tb.append(pendingThought); pendingThought = "";
      if (atEnd) tb.scrollTop = tb.scrollHeight;        // follow the thinking unless the user scrolled up in it
    }
    if (planWrap) planWrap.innerHTML = chPlanHTML(bot);
    while (roundsWrap && renderedRounds < (bot.rounds?.length || 0)) {
      roundsWrap.insertAdjacentHTML("beforeend", chRoundItemHTML(bot.rounds[renderedRounds++]));
    }
    if (noteEl) {
      noteEl.hidden = !curNote;
      if (curNote) noteEl.innerHTML = md(curNote);
    }
    if (liveToolsEl) {
      const openSteps = new Set([...liveToolsEl.querySelectorAll("details.chattoolstep[open]")].map((d) => d.dataset.stepId));
      liveToolsEl.innerHTML = chToolStepsInner(curTools, openSteps);
    }
    if (cardWrap) {
      const cardKey = `${bot.pendingApproval?.id || ""}|${bot.pendingQuestion?.id || ""}`;
      if (cardKey !== lastCardKey) {
        lastCardKey = cardKey;
        cardWrap.innerHTML = chHarnessCardHTML(bot);
      }
    }
    if (bot.switching) contentEl.innerHTML = `<span class="shimmer">${bot.switching === "vision" ? `Loading ${esc(bot.switchModel || "the model")} with vision… (about 10 s)` : `Loading ${esc(bot.switchModel || "the model")}… (first load takes 10–30 s)`}</span>`;
    else if (bot.content) contentEl.innerHTML = md(bot.content) + '<span class="caret"></span>';
    else if (bot.reasoning || curTools.length || bot.rounds?.length) contentEl.innerHTML = "";
    if (view === "chat" || view === "code") scrollBottom();
  };
  // rAF pauses while the window is hidden or covered; fall back to a timer so the reply keeps
  // rendering (and is complete when the user comes back).
  const repaint = () => { if (!raf) raf = document.hidden ? -setTimeout(paint, 150) : requestAnimationFrame(paint); };
  try {
    const res = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: streaming.signal });
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const evt = buf.slice(0, i); buf = buf.slice(i + 2);
        for (const line of evt.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim(); if (data === "[DONE]") continue;
          let j; try { j = JSON.parse(data); } catch { continue; }
          if (j.error) { bot.error = typeof j.error === "string" ? j.error : (j.error.message || JSON.stringify(j.error)); continue; }
          if (j.x_engine) { bot.switching = j.x_engine.status === "switching" ? j.x_engine.mode : null; bot.switchModel = j.x_engine.model; if (bot.switching) poll(); repaint(); continue; }
          if (j.x_sources) { bot.sources = [...(bot.sources || []), ...j.x_sources]; continue; }
          if (j.x_web_sources) { bot.web_sources = [...(bot.web_sources || []), ...j.x_web_sources]; continue; }
          if (j.x_plan) {
            bot.plan = j.x_plan;
            repaint();
            continue;
          }
          if (j.x_approval) {
            bot.pendingApproval = j.x_approval;
            repaint(); updateSend();
            scrollBottom(true);
            continue;
          }
          if (j.x_question) {
            bot.pendingQuestion = j.x_question;
            repaint(); updateSend();
            scrollBottom(true);
            continue;
          }
          if (j.x_tool_output) {
            const tid = j.x_tool_output.id, chunk = j.x_tool_output.chunk || "";
            const step = curTools.find((s) => s.id === tid) || (bot.tool_steps || []).find((s) => s.id === tid);
            if (step && chunk) {
              step.detail = (step.detail ? step.detail + "\n" : "") + chunk;
              repaint();
            }
            continue;
          }
          if (j.x_checkpoint) {
            bot.checkpoint = j.x_checkpoint;
            continue;
          }
          if (j.x_tool) {
            const rNo = String(j.x_tool.id || "").split("-")[0];
            if (curTools.length && curRoundNo !== null && rNo !== curRoundNo) advanceRound();
            curRoundNo = rNo;
            finishRoundThinking();
            if (!curTools.length && bot.content.trim()) {
              curNote = bot.content.trim();
              bot.content = "";
            }
            const ci = curTools.findIndex((step) => step.id === j.x_tool.id);
            if (ci < 0) curTools.push(j.x_tool); else curTools[ci] = j.x_tool;
            bot.tool_steps ||= [];
            const i = bot.tool_steps.findIndex((step) => step.id === j.x_tool.id);
            if (i < 0) bot.tool_steps.push(j.x_tool); else bot.tool_steps[i] = j.x_tool;
            if (bot.pendingApproval && bot.pendingApproval.id === j.x_tool.id) delete bot.pendingApproval;
            if (bot.pendingQuestion && bot.pendingQuestion.id === j.x_tool.id) delete bot.pendingQuestion;
            if (view === "code" && j.x_tool.status === "done" && (CODE_MUTATING.includes(j.x_tool.name) || String(j.x_tool.name).startsWith("mcp__"))) onAgentCodeMutation();
            delete bot.finish; repaint(); continue;
          }
          if (j.x_agent && AG_RID.test(j.x_agent.run_id || "")) { actionRun = j.x_agent.run_id; continue; }
          if (j.x_computer_setup) {
            bot.content = j.x_computer_setup.message; bot.computerSetup = j.x_computer_setup.enable ? "enable" : "settings";
            continue;
          }
          const ch = j.choices?.[0], d = ch?.delta;
          if (ch?.finish_reason) bot.finish = ch.finish_reason;
          const rc = d?.reasoning_content ?? d?.reasoning;
          if (rc) {
            if (curTools.length) advanceRound();
            if (!thinkStart) { thinkStart = performance.now(); th.hidden = false; tl.className = "tl shimmer"; tl.textContent = "Thinking…"; if (!userToggled) setOpen(true); }
            bot.reasoning += rc; pendingThought += rc;
          }
          if (d?.content) {
            if (curTools.length) advanceRound();
            finishRoundThinking();
            bot.content += d.content;
          }
          if (j.x_mlx_dspark) bot.stats = j.x_mlx_dspark;
          repaint();
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError" && actionRun) {
      try { await jpost(`/api/agent/runs/${actionRun}/stop`); } catch {}
    }
    bot.error = e.name === "AbortError" ? (bot.content ? "" : "Stopped.") : String(e.message || e);
  }
  if (raf) { raf < 0 ? clearTimeout(-raf) : cancelAnimationFrame(raf); paint(); }
  if (thinkStart && !thinkEnd) bot.thoughtSecs = (performance.now() - thinkStart) / 1000;
  if (curTools.length) advanceRound();
  if (!bot.error) delete bot.error;
  delete bot.switching; delete bot.switchModel;
  delete bot.pendingApproval; delete bot.pendingQuestion;
  bot.thoughtOpen = th.open;                               // keep the panel as the user left it
  const openStepIds = new Set([...el.querySelectorAll("details.chattoolstep[open]")].map((d) => d.dataset.stepId));
  el.querySelectorAll(".chroundswrap .chround").forEach((rEl, rIdx) => {
    const rTh = rEl.querySelector("details.thought");
    if (rTh && bot.rounds?.[rIdx]) bot.rounds[rIdx].thoughtOpen = rTh.open;
  });
  streaming = null; liveGen = null; updateSend(); renderHistory();
  if (actionRun) return chAttachAction(chat, bot, el, actionRun);
  // Swap in the finished turn (actions + stats) without touching the rest of the thread.
  const tbScroll = tb.scrollTop;
  const tmp = document.createElement("div"); tmp.innerHTML = turnHTML(bot, idx);
  const done = tmp.firstElementChild;
  done.querySelectorAll("details.chattoolstep").forEach((d) => {
    if (openStepIds.has(d.dataset.stepId)) d.open = true;
  });
  if (el.isConnected) {
    el.replaceWith(done);
    const ntb = done.querySelector(":scope > .thought .tb"); if (ntb) ntb.scrollTop = tbScroll;
    showPerf(bot.stats); drawSpark(); scrollBottom();
  }
  if (current === chat) updateCtx();
  if (!chat.deleted) await saveChat(chat);
  poll();
}

// ---------------- computer actions in chat
async function chShowSteps(button, rid) {
  const turn = button.closest(".turn.bot");
  const existing = turn.querySelector(".chsavedsteps");
  if (existing) { existing.hidden = !existing.hidden; return; }
  button.disabled = true;
  try {
    const data = await api(`/api/agent/runs/${rid}`);
    const box = document.createElement("div"); box.className = "aglog chsavedsteps";
    const state = { ...agFresh(), rid, log: box, replay: true, events: data.events || [] };
    for (const event of state.events) if (event.type !== "answer") agRender(state, event);
    agDerive(state);
    box.addEventListener("click", (event) => agLogClick(state, event));
    turn.append(box);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}
// The Agent switch: a message becomes a task for the Agent. Its card lives in the thread (same building blocks as the Agent view, see agFresh);
// when the run ends the card is replaced by the run's answer as an ordinary reply that links back to the steps.
const AG_RID = /^[a-f0-9]{12}$/, CH_STATUS = { done: "Done", stopped: "Stopped", failed: "Failed", interrupted: "Interrupted" };
// The last few turns of the chat as plain text for the Agent, newest last, at most ~6000 characters.
function chContextText(msgs) {
  const lines = [];
  for (const m of msgs) {
    if (!m || (m.role !== "user" && m.role !== "assistant")) continue;
    const t = String(m.content || "").trim(); if (!t) continue;
    lines.push(`${m.role === "user" ? "User" : "Assistant"}: ${t.length > 2000 ? t.slice(0, 2000) + "…" : t}`);
  }
  let out = "";
  for (const line of lines.slice(-6).reverse()) {
    const next = out ? line + "\n" + out : line;
    if (next.length > 6000) break;
    out = next;
  }
  return out;
}
const chCardHTML = (i) => `<div class="turn bot agturn" data-i="${i}"><div class="agchat">
  <div class="agchst"><span class="dot ok"></span><span class="agchtx">Starting…</span></div>
  <div class="agchshot" hidden></div>
  <details class="thought agchsteps" hidden><summary>Steps <span class="tw"></span>${icon("i-right")}</summary><div class="tb"><div class="aglog"></div></div></details>
  <div class="agchcards"></div>
  <div class="agchctl"><button class="btn sm" data-chtake>Take over</button><button class="btn sm danger" data-chstop>Stop</button></div>
</div></div>`;
function chNewRun(chat, msg) {
  const c = { chat, msg, starting: false, el: null, ui: null, shotShown: undefined, s: null };
  c.s = { ...agFresh(), es: null, timer: 0, replay: false, log: null, cardBox: null, answerCard: false,
    scroller: () => stick ? $("chatScroll") : null,
    hooks: { sync: () => chSync(c), ended: () => chEnded(c), retry: () => chRetry(c) } };
  return c;
}
// Point the run at a (new) card element. What it has received so far is drawn again into it.
function chBind(c, el) {
  const s = c.s, root = el.querySelector(".agchat");
  c.el = el; c.shotShown = undefined;
  c.ui = { dot: root.querySelector(".dot"), tx: root.querySelector(".agchtx"), shot: root.querySelector(".agchshot"), steps: root.querySelector(".agchsteps"), count: root.querySelector(".tw"),
    ctl: root.querySelector(".agchctl"), take: root.querySelector("[data-chtake]"), stop: root.querySelector("[data-chstop]") };
  s.log = root.querySelector(".aglog"); s.cardBox = root.querySelector(".agchcards");
  Object.assign(s, { cards: {}, urls: [], shot: null, where: "", answer: "", engine: "" });
  const was = s.replay; s.replay = true;
  for (const e of s.events) agRender(s, e);
  s.replay = was;
  chSync(c);
}
function chSync(c) {
  const s = c.s, ui = c.ui; if (!ui) return;
  agDerive(s);
  let [txt, dot] = agStateText(s, "Running…", "Starting…");
  if (!s.live && s.status) txt = CH_STATUS[s.status] || txt;
  if (s.lost) [txt, dot] = ["Lost connection. Reconnecting…", "warn"];
  ui.tx.textContent = txt; ui.dot.className = "dot " + dot;
  const url = s.shot && s.rid ? agShotUrl(s, s.shot) : "";
  if (url !== c.shotShown) {
    c.shotShown = url; ui.shot.hidden = !url;
    ui.shot.innerHTML = url ? `<img src="${esc(url)}" alt="Latest screenshot" data-full="${esc(url)}" title="Open full size">` : "";
  }
  const steps = s.log.querySelectorAll(".agstep").length;
  ui.steps.hidden = !s.log.childElementCount; ui.count.textContent = steps ? `· ${steps}` : "";
  ui.ctl.hidden = !s.live;
  ui.take.disabled = !s.live || s.pausing || s.stopping; ui.take.textContent = s.paused ? "Resume" : s.pausing ? "Pausing…" : "Take over";
  ui.stop.disabled = !s.live || s.stopping; ui.stop.textContent = s.stopping ? "Stopping…" : "Stop";
  agAttnNow(); updateSend();
}
function chCardClick(c, e) {
  if (e.target.closest("[data-chstop]")) return agStopRun(c.s);
  if (e.target.closest("[data-chtake]")) return agTakeoverRun(c.s);
  const img = e.target.closest(".agchshot img[data-full]");
  if (img) { window.open(img.dataset.full, "_blank"); return; }
  agLogClick(c.s, e);
}
function chStop() { if (!chRun?.s.rid) return toast("The Agent is still starting"); agStopRun(chRun.s); }
// Leave the card behind (another chat opens): the task keeps running, and opening this chat again reattaches to it.
function chDetach() { if (chRun) { agClose(chRun.s); chRun = null; agAttnNow(); updateSend(); } }
async function chAttachAction(chat, bot, el, rid) {
  bot.content = ""; bot.agent_run = rid;
  delete bot.computerSetup; delete bot.error; delete bot.finish;
  await saveChat(chat);                                      // a reload can recover the run even before its first event
  if (current !== chat) return;
  const c = chRun = chNewRun(chat, bot);
  Object.assign(c.s, { rid, live: true, task: chat.messages.at(-2)?.content || "" });
  const card = chSwapTurn(chat, bot, el);
  if (card) chBind(c, card);
  agConnect(c.s); chSync(c); loadAgentRuns(); scrollBottom(true);
}
// Redraw one assistant turn where it stands. Never re-renders the thread: a normal reply may be streaming into it in this same chat.
function chSwapTurn(chat, m, prefer) {
  if (current !== chat) return null;
  const i = chat.messages.indexOf(m); if (i < 0) return null;
  const old = (prefer && prefer.isConnected ? prefer : null) || $("thread").querySelector(`.turn.bot[data-i="${i}"]`);
  const tmp = document.createElement("div"); tmp.innerHTML = turnHTML(m, i);
  const el = tmp.firstElementChild;
  if (old) { old.replaceWith(el); return el; }
  if (streaming) return null;
  renderThread();
  return $("thread").querySelector(`.turn.bot[data-i="${i}"]`);                // the element now in the page
}
async function chEnded(c) {
  if (chRun !== c) return;
  chRun = null;
  loadAgentRuns(); loadAgent();
  const m = c.msg;
  m.content = agAnswerMd(agFinalText(agOutcome(c.s.events))); m.agent_run = c.s.rid;
  chSwapTurn(c.chat, m, c.el); scrollBottom();
  updateSend(); agAttnNow();
  await saveChat(c.chat);
}
async function chRetry(c) {                                  // lost the stream: look at the task, then follow it again or show how it ended
  const s = c.s; if (!s.lost) return;
  let d; try { d = await api(`/api/agent/runs/${s.rid}`); } catch { return; }
  if (!s.lost || chRun !== c) return;
  const evs = Array.isArray(d.events) ? d.events : [];
  s.lost = false;
  evs.forEach((e) => agEvent(s, e));
  if (!evs.some((e) => e?.type === "end")) {
    if (["done", "stopped", "failed", "interrupted"].includes(d.status)) agEvent(s, { i: evs.length, type: "end", status: d.status });
    else { s.live = true; s.status = ""; agConnect(s); chSync(c); }
  }
}
// A saved chat whose last Agent task had no result yet: follow it if it is still running, else fill in how it ended.
async function chResume() {
  const chat = current, ms = chat?.messages || [];
  let i = -1; for (let k = ms.length - 1; k >= 0; k--) if (ms[k].role === "assistant" && typeof ms[k].agent_run === "string" && !ms[k].content && !ms[k].error) { i = k; break; }
  if (i < 0) return;
  const m = ms[i], rid = m.agent_run; if (!AG_RID.test(rid)) return;
  let d; try { d = await api(`/api/agent/runs/${rid}`); } catch { return; }
  const evs = Array.isArray(d.events) ? d.events : [];
  let live = false;
  if (!evs.some((e) => e?.type === "end")) { const st = await agFetchStatus(); if (!st) return; live = st.running === rid; }   // can't tell: leave it for the next open
  if (current !== chat || m.content || chRun) return;
  if (live) {
    const c = chRun = chNewRun(chat, m);
    Object.assign(c.s, { rid, live: true, task: d.task || "" });
    const el = chSwapTurn(chat, m, null);                    // draws the card for chRun in place
    if (el && chRun === c && el.querySelector(".agchat")) chBind(c, el);
    agConnect(c.s); updateSend(); scrollBottom(true);
    return;
  }
  const final = ["done", "stopped", "failed", "interrupted"].includes(d.status) ? d.status : "interrupted";   // not live and not over: it was cut off
  m.content = agAnswerMd(agFinalText(agOutcome(evs, final)));
  chSwapTurn(chat, m, null); await saveChat(chat);
}

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
const activeCodeWs = () => current?.workspace || codeWorkspace || "";
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
    if (info.workspace && !codeWorkspace) codeWorkspace = info.workspace;
    if (current && info.workspace && !current.workspace) { current.workspace = info.workspace; updateWorkspaceUI(); }
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
    codeWorkspace = r.path; store.set("code_workspace", r.path);
    if (current) { current.workspace = r.path; updateWorkspaceUI(); if (current.messages.length) await saveChat(current); }
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
    if (streaming || chLive()) return toast("Wait for the reply to finish");
    const cmd = cmdBtn.dataset.codeCmd;
    if (cmd === "/compact") return compactCurrentChat();
    const ws = activeCodeWs();
    if (!current) current = { id: newId(), title: cmd, messages: [] };
    if (ws) current.workspace = ws;
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
let lib = null, libTab = "chat", runningName = null, runningMode = null;
const activeChat = () => lib?.chat.find((m) => m.active);
const activeImage = () => lib?.image.find((m) => m.active);
function updateHeader() {
  if (view === "chat") {
    const a = activeChat();
    $("hdrTitle").textContent = runningName || a?.name || "Local Studio";
    $("hdrSub").textContent = runningMode === "vision" ? "Vision" : runningMode === "gguf" ? "llama.cpp" : (a?.accel || "");
  } else if (view === "code") { $("hdrTitle").textContent = "Code"; $("hdrSub").textContent = runningName || activeChat()?.name || ""; }
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
  if (!current?.messages?.length && view === "chat" && (noModel || loadLibrary._noModel)) renderThread();   // onboarding only
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
$("mmList").onclick = async (e) => { const r = e.target.closest("[data-mm]"); if (!r) return; closeMenus(); if (streaming) return toast("Wait for the reply to finish"); try { await setActive(r.dataset.mm); } catch (err) { toast(err.message); } };
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
    if (b.dataset.clear === "chats") { chDetach(); current = null; loadChats(); }
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
let sr = null, srTimer = 0, srSeq = 0, useFiles = store.get("useFiles", false), noFilesOnce = false;
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
function agAttnNow() { const w = (s) => !!s && s.live && !!s.pending && !s.paused; agAttn(w(ag) || ag.bgWait || w(chRun?.s)); }
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
  if (streaming) return toast("Wait for the reply to finish");
  if (!ag.answer) return;
  const task = ag.task || $("agTask").value.trim();
  if (ag.chatId) {                                            // a task that began in a chat: go back to that chat, which already has the answer
    let ok = false; try { await api(`/api/chats/${encodeURIComponent(ag.chatId)}`); ok = true; } catch {}
    if (ok) return openChat(ag.chatId);
  }
  chDetach();
  current = { id: newId(), title: task.replace(/\s+/g, " ").slice(0, 48) || "Agent task", messages: [{ role: "user", content: task }, { role: "assistant", content: agAnswerMd(ag.answer) }] };
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
  await api("/api/system").then((s) => { $("verBadge").textContent = s.version; sysRam = s.total_bytes; }).catch(() => {});
  await loadLibrary();
  loadSearch();
  loadAgent();
  showView("chat");
  renderThread();
  await loadChats();
  poll();
})();

export {
  $, acIndex, acItems, acMode, acStart, acTimer, activeChat, activeCodeWs, activeImage, activeJob, addFiles, ag,
  AG_CUA_MIN, AG_CUA_URL, AG_DROP, AG_EXAMPLES, AG_RID, AG_STATUS, AG_TAGS, AG_TIER, AG_TIER_ORDER, AG_TITLE,
  agAnswerCard, agAnswerMd, agApprovalCard, agAttach, agAttn, agAttnNow, agBgCheck, agBoxOf, agCardEl, agClean,
  agClear, agClose, agConnect, agContinue, agDecision, agDerive, agDetail, agEvent, agFetchStatus, agFinalText,
  agFresh, agGrant, agLimit, agLogClick, agLogKey, agLost, agMd, agNoImages, agNoteEl, agOpen, agOpenSaved, agOutcome,
  agPausedOf, agPendingOf, agPermMissing, agQuestionCard, agRender, agRenderModels, agRenderRuns, agRenderSettings,
  agRenderSetup, agRenderTiers, agReply, agResolve, agSave, agSetErr, agSetTier, agShotUrl, agSize, agStart,
  agStateText, agStepRow, agStopRun, agSync, agTakeoverRun, agTierKey, agTierSelect, agTitleHTML, agWeb, agWhen, api,
  applyAc, applyTheme, attMeta, attThumb, autosize, autoTop, cardHTML, cardSig, CH_STATUS, CHAT_DROP, chatHost, chats,
  chAttachAction, chatView, chBind, chCardClick, chCardHTML, chContextText, chDetach, chEnded, chHarnessCardHTML,
  chLive, chNewRun, chPlanHTML, chResume, chRetry, chRoundItemHTML, chRoundsHTML, chRun, chShowSteps, chStop,
  chSwapTurn, chSync, chToolsHTML, chToolStepsInner, chWebSourcesHTML, cleanChat, closeAc, closeMenus, CODE_CHECKED,
  CODE_MUTATING, CODE_STARTERS, codeCloseFile, codeConfirmDiscard, codeDirty, codeEdViewMode, codeEmpty, codeExSeq,
  codeExTab, codeFileDiff, codeFullDiff, codeGitInfo, codeInstFiles, codeJump, codeMarkActive, codeOpenPath,
  codeOriginalContent, codeSelectExTab, codeShowAllChanges, codeShowDiag, codeSyncDirty, codeTermFit, codeTermLabel,
  codeTermS, codeTermSend, codeTermStart, codeTermStream, codeTermTheme, codeUpdateStatus, codeWorkspace, codeWsQ,
  compactCurrentChat, confirmBox, ctxUsage, ctxWindow, current, DEFAULT_MAX_TOKENS, deleteImage, diffLinesHTML, dlText,
  DOC_ICON, dragDepth, drawSpark, EDIT_TOOLS, effort, engineState, esc, fmtSecs, fmtTok, gallery, generate, gib,
  groupOf, health, hfKind, hfSearch, human, icon, jpost, lbItem, lib, libTab, liveGen, liveSkeletonHTML, loadAgent,
  loadAgentRuns, loadChats, loadCodeProcesses, loadCodeView, loadGallery, loadLibrary, loadManage, loadMcp, loadSearch,
  loadStorage, loadVersions, loadXterm, mcpServers, md, metrics, modelThinks, newChat, newId, noFilesOnce,
  onAgentCodeMutation, openChat, openCodeFile, openLightbox, patchCards, pending, perfGrid, perfLine, PERM_WORDS,
  permMode, poll, renderAc, renderCodeExplorer, renderDiffLinesHTML, renderHistory, renderImgModels, renderLibrary,
  renderMenu, renderOnboarding, renderPending, renderThread, runningMode, runningName, runSearch, safeMd, saveChat,
  saveCodeFile, scrollBottom, send, setActive, setCodeEditorMode, setCollapsed, setStepOptions, showPerf, showView,
  SLASH_CMDS, sourcesHTML, sr, SR_ICON, SR_OFF, SR_PAUSED, SR_STOP, srAddFolder, srAuto, srClosePreview, srEnable,
  srHome, srLoc, srMark, srOpen, srPvSeq, srPvState, srRemoveFolder, srRes, srRow, srSave, srSecs, srSel, srSelect,
  srSeq, srShowPage, srStatusLine, srTimer, STARTERS, stick, STORAGE, store, streaming, syncChatDock, syncEffort,
  syncFiles, syncMode, syncThink, sysRam, tagsHTML, thinking, thoughtHTML, tickWork, toast, toggleEngine, toggleMode,
  TOOL_LABELS, toolLabel, toolResultHTML, track, turnHTML, updateAc, updateCodeGutter, updateCtx, updateHeader,
  updateSend, updateWorkspaceUI, useFiles, view, workStart, workTimer, XTERM_FILES, xtermLoad
};
