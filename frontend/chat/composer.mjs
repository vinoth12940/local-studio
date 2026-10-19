// chat/composer.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import {
  $, activeChat, api, attachmentsState, chLive, chStop, codeUpdateStatus, esc, jpost, listState, liveGen, modelThinks,
  openChat, renderThread, saveChat, send, store, toast, view
} from "../modules.mjs";

let thinking = store.get("think", true), effort = store.get("effort", "");

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
        const ws = listState.current?.workspace ? `&workspace=${encodeURIComponent(listState.current.workspace)}` : "";
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
  if (e.key === "Tab" && e.shiftKey && !e.isComposing && (listState.current?.workspace || view === "code")) { e.preventDefault(); toggleMode(); return; }
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
function ctxUsage(chat = listState.current) {
  const msgs = chat?.messages || [];
  if (!msgs.length) return null;
  let i = msgs.length - 1;
  while (i >= 0 && !msgs[i].stats?.prompt_tokens) i--;
  let used = i >= 0 ? (msgs[i].stats.prompt_tokens || 0) + (msgs[i].stats.completion_tokens || 0) : 2000;
  for (const m of msgs.slice(i + 1)) used += Math.ceil((m.content || "").length / 3.2) + (m.attachments || []).reduce((a, x) => a + (x.tokens_est || (x.kind === "image" ? 1200 : 0)), 0);
  return { used, total: listState.ctxWindow, measured: i === msgs.length - 1 };
}
const fmtTok = (n) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
function updateCtx() {
  const u = ctxUsage(), f = $("ctxFoot");
  if (!u) { f.hidden = true; return; }
  const pct = Math.min(100, Math.round(u.used / u.total * 100)), free = Math.max(0, u.total - u.used);
  const level = pct >= 90 ? "bad" : pct >= 70 ? "warn" : "";
  const canCompact = (listState.current?.messages?.length || 0) > 4;
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
  const secs = Math.round((Date.now() - workStart) / 1000), elsewhere = liveGen && liveGen.chat !== listState.current;
  const waiting = liveGen ? !!(liveGen.bot.pendingApproval || liveGen.bot.pendingQuestion) : !!document.querySelector("#thread .chhcard");
  $("workTxt").textContent = elsewhere ? `${waiting ? "Waiting for you" : "Working"} in “${liveGen.chat.title}”` : waiting ? "Waiting for you…" : "Working…";
  $("workHint").innerHTML = elsewhere ? `· <button class="workopen" data-workopen>Open</button>` : `· <kbd>esc</kbd> to interrupt`;
  $("workTime").textContent = secs >= 1 ? `${secs < 60 ? secs + "s" : Math.floor(secs / 60) + "m " + (secs % 60) + "s"}` : "";
}
function updateSend() {
  const here = !liveGen || liveGen.chat === listState.current;
  const b = $("send"), working = (!!listState.streaming && here) || chLive();
  const wl = $("workLine"), anyWork = !!listState.streaming || chLive();
  if (anyWork && wl.hidden) { wl.hidden = false; workStart = Date.now(); clearInterval(workTimer); workTimer = setInterval(tickWork, 1000); }
  else if (!anyWork && !wl.hidden) { wl.hidden = true; clearInterval(workTimer); }
  if (anyWork) tickWork();
  $("sendIcon").setAttribute("href", working ? "#i-stop" : "#i-up");
  b.title = working ? "Stop" : "Send";
  const ready = attachmentsState.pending.filter((p) => p.meta).length, busy = attachmentsState.pending.some((p) => p.busy);
  b.disabled = !working && (busy || (!$("input").value.trim() && !ready));
}
$("send").onclick = () => listState.streaming && (!liveGen || liveGen.chat === listState.current) ? listState.streaming.abort() : chLive() ? chStop() : send();
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
  const stoppable = (listState.streaming && (!liveGen || liveGen.chat === listState.current)) || chLive();
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
  if (!listState.current || (listState.current.messages || []).length <= 4) { toast("Conversation is already compact"); return; }
  try {
    const r = await jpost("/api/chat/compact", { chat_id: listState.current.id, messages: listState.current.messages, keep_recent: 4 });
    if (r.compacted && Array.isArray(r.messages)) {
      listState.current.messages = r.messages;
      renderThread();
      await saveChat(listState.current);
      toast(`Compacted ${(r.before_count || 0) - (r.after_count || 0) + 1} earlier messages`);
    } else {
      toast("Conversation is already compact");
    }
  } catch (err) { toast(err.message || "Compaction failed"); }
}
$("compactBtn").onclick = compactCurrentChat;

export {
  acIndex, acItems, acMode, acStart, acTimer, applyAc, autosize, closeAc, compactCurrentChat, ctxUsage, effort, fmtTok,
  permMode, renderAc, SLASH_CMDS, syncEffort, syncMode, syncThink, thinking, tickWork, toggleMode, updateAc, updateCtx,
  updateSend, workStart, workTimer
};
