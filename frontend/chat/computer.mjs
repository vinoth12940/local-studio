// chat/computer.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import {
  $, agAnswerMd, agAttnNow, agClose, agConnect, agDerive, agEvent, agFetchStatus, agFinalText, agFresh, agLogClick,
  agOutcome, agRender, agShotUrl, agStateText, agStopRun, agTakeoverRun, api, esc, icon, listState, loadAgent,
  loadAgentRuns, renderThread, saveChat, scrollBottom, threadState, toast, turnHTML, updateSend
} from "../modules.mjs";

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
    scroller: () => threadState.stick ? $("chatScroll") : null,
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
function chStop() { if (!listState.chRun?.s.rid) return toast("The Agent is still starting"); agStopRun(listState.chRun.s); }
// Leave the card behind (another chat opens): the task keeps running, and opening this chat again reattaches to it.
function chDetach() { if (listState.chRun) { agClose(listState.chRun.s); listState.chRun = null; agAttnNow(); updateSend(); } }
async function chAttachAction(chat, bot, el, rid) {
  bot.content = ""; bot.agent_run = rid;
  delete bot.computerSetup; delete bot.error; delete bot.finish;
  await saveChat(chat);                                      // a reload can recover the run even before its first event
  if (listState.current !== chat) return;
  const c = listState.chRun = chNewRun(chat, bot);
  Object.assign(c.s, { rid, live: true, task: chat.messages.at(-2)?.content || "" });
  const card = chSwapTurn(chat, bot, el);
  if (card) chBind(c, card);
  agConnect(c.s); chSync(c); loadAgentRuns(); scrollBottom(true);
}
// Redraw one assistant turn where it stands. Never re-renders the thread: a normal reply may be streaming into it in this same chat.
function chSwapTurn(chat, m, prefer) {
  if (listState.current !== chat) return null;
  const i = chat.messages.indexOf(m); if (i < 0) return null;
  const old = (prefer && prefer.isConnected ? prefer : null) || $("thread").querySelector(`.turn.bot[data-i="${i}"]`);
  const tmp = document.createElement("div"); tmp.innerHTML = turnHTML(m, i);
  const el = tmp.firstElementChild;
  if (old) { old.replaceWith(el); return el; }
  if (listState.streaming) return null;
  renderThread();
  return $("thread").querySelector(`.turn.bot[data-i="${i}"]`);                // the element now in the page
}
async function chEnded(c) {
  if (listState.chRun !== c) return;
  listState.chRun = null;
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
  if (!s.lost || listState.chRun !== c) return;
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
  const chat = listState.current, ms = chat?.messages || [];
  let i = -1; for (let k = ms.length - 1; k >= 0; k--) if (ms[k].role === "assistant" && typeof ms[k].agent_run === "string" && !ms[k].content && !ms[k].error) { i = k; break; }
  if (i < 0) return;
  const m = ms[i], rid = m.agent_run; if (!AG_RID.test(rid)) return;
  let d; try { d = await api(`/api/agent/runs/${rid}`); } catch { return; }
  const evs = Array.isArray(d.events) ? d.events : [];
  let live = false;
  if (!evs.some((e) => e?.type === "end")) { const st = await agFetchStatus(); if (!st) return; live = st.running === rid; }   // can't tell: leave it for the next open
  if (listState.current !== chat || m.content || listState.chRun) return;
  if (live) {
    const c = listState.chRun = chNewRun(chat, m);
    Object.assign(c.s, { rid, live: true, task: d.task || "" });
    const el = chSwapTurn(chat, m, null);                    // draws the card for chRun in place
    if (el && listState.chRun === c && el.querySelector(".agchat")) chBind(c, el);
    agConnect(c.s); updateSend(); scrollBottom(true);
    return;
  }
  const final = ["done", "stopped", "failed", "interrupted"].includes(d.status) ? d.status : "interrupted";   // not live and not over: it was cut off
  m.content = agAnswerMd(agFinalText(agOutcome(evs, final)));
  chSwapTurn(chat, m, null); await saveChat(chat);
}

export {
  AG_RID, CH_STATUS, chAttachAction, chBind, chCardClick, chCardHTML, chContextText, chDetach, chEnded, chNewRun,
  chResume, chRetry, chShowSteps, chStop, chSwapTurn, chSync
};
