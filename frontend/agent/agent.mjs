// agent/agent.mjs: moved from index.html's inline script in the refactor's P2 split.
import { $, api, confirmBox, esc, icon, jpost, newId, store, toast } from "../core/util.mjs";
import { agAnswerMd } from "../core/markdown.mjs";
import { showView, view } from "../ui/views.mjs";
import { listState, openChat, saveChat } from "../chat/list.mjs";
import { renderThread, scrollBottom } from "../chat/thread.mjs";
import { chDetach } from "../chat/computer.mjs";
import { lib, loadLibrary } from "../library/library.mjs";
import {
  AG_CUA_MIN, AG_CUA_URL, AG_EXAMPLES, AG_STATUS, AG_TITLE, agBoxOf, agMd, agNoteEl, agPausedOf, agPendingOf,
  agShotUrl, agTitleHTML, agWeb, agWhen
} from "./helpers.mjs";
import { ag, agFresh, agPermMissing } from "./state.mjs";
import { agGrant, agRenderSettings } from "../modules.mjs";

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

export {
  agAnswerCard, agApprovalCard, agAttach, agAttn, agAttnNow, agBgCheck, agCardEl, agClear, agClose, agConnect,
  agContinue, agDecision, agDerive, agDetail, agEvent, agFetchStatus, agLogClick, agLogKey, agLost, agOpen,
  agOpenSaved, agQuestionCard, agRender, agRenderModels, agRenderRuns, agRenderSetup, agReply, agResolve, agSetErr,
  agSize, agStart, agStateText, agStepRow, agStopRun, agSync, agTakeoverRun, loadAgent, loadAgentRuns
};
