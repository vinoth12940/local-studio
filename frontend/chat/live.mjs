// chat/live.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { $, DEFAULT_MAX_TOKENS, esc, icon, jpost, newId, store, toast } from "../core/util.mjs";
import { chatEvents } from "./stream.mjs";
import { md } from "../core/markdown.mjs";
import { view } from "../ui/views.mjs";
import { poll } from "../ui/engine.mjs";
import { chLive, listState, renderHistory, saveChat, updateWorkspaceUI } from "./list.mjs";
import {
  chHarnessCardHTML, chPlanHTML, chRoundItemHTML, chToolStepsInner, drawSpark, fmtSecs, modelThinks, renderThread,
  scrollBottom, showPerf, threadState, turnHTML
} from "./thread.mjs";
import { attachmentsState, renderPending } from "./attachments.mjs";
import {
  autosize, closeAc, compactCurrentChat, effort, permMode, thinking, updateCtx, updateSend
} from "./composer.mjs";
import { activeCodeWs, AG_RID, chAttachAction, CODE_MUTATING, loadCodeView, onAgentCodeMutation, searchState, sr, useFiles } from "../modules.mjs";

async function send() {
  const text = $("input").value.trim();
  const atts = attachmentsState.pending.filter((p) => p.meta).map((p) => p.meta);
  if ((text || atts.length) && listState.streaming && liveGen && liveGen.chat !== listState.current) return toast(`Still answering in “${liveGen.chat.title}”. Wait for it, or open it and stop it.`);
  if ((!text && !atts.length) || listState.streaming || chLive() || attachmentsState.pending.some((p) => p.busy)) return;
  if (view === "code" && !activeCodeWs()) await loadCodeView();
  const workspace = view === "code" ? activeCodeWs() : listState.current?.workspace;
  if (view === "code" && !workspace) return toast("Choose a workspace folder before sending a coding task.");
  if (workspace) {
    if (!listState.current) listState.current = { id: newId(), title: "New coding task", messages: [] };
    listState.current.workspace = workspace;
    updateWorkspaceUI();
  }
  closeAc();
  if (text === "/compact" || text.startsWith("/compact ")) {
    $("input").value = ""; autosize();
    await compactCurrentChat();
    return;
  }
  $("input").value = ""; attachmentsState.pending = []; renderPending(); autosize();
  const title = (text || atts.map((a) => a.name).join(", ")).replace(/\s+/g, " ").slice(0, 48);
  if (!listState.current) listState.current = { id: newId(), title, messages: [] };
  else if (!listState.current.messages.length) listState.current.title = title;                            // a placeholder ("New coding task", "New chat") until the first message
  listState.current.messages.push({ role: "user", content: text, attachments: atts.length ? atts : undefined });
  saveChat(listState.current);                                                                    // in the sidebar now, so the user can leave and come back mid-reply
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
async function generate(resumeBot = null) {
  if (resumeBot && (listState.streaming || !resumeBot.stream_id)) return;
  const chat = listState.current;
  const bot = resumeBot || { role: "assistant", content: "", reasoning: "" };
  // Replay rebuilds the reply from event zero; keep the saved version until it completes.
  const savedReply = resumeBot ? structuredClone(resumeBot) : null;
  if (!resumeBot) chat.messages.push(bot);
  else {
    for (const key of Object.keys(bot)) if (!["role", "stream_id", "stream_pending"].includes(key)) delete bot[key];
    bot.content = ""; bot.reasoning = "";
  }
  const idx = chat.messages.indexOf(bot);
  if (resumeBot) $("thread").querySelector(`.turn.bot[data-i="${idx}"]`)?.remove();
  // Stable DOM for the live reply: the thinking panel and answer are updated in place, so the
  // panel keeps its open/closed state and its own scroll position while tokens stream in.
  $("thread").insertAdjacentHTML("beforeend", liveSkeletonHTML(idx, listState.current.messages.some((x) => x.attachments?.length) ? "Reading attachments…" : "Thinking…"));
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
    const node = listState.current === chat && $("thread").querySelector(`.turn.bot[data-live="1"][data-i="${chat.messages.indexOf(bot)}"]`);
    if (node && node !== el) { bind(node); if (raf) { raf < 0 ? clearTimeout(-raf) : cancelAnimationFrame(raf); } paint(); }
  };
  liveGen = { chat, bot, attach };
  threadState.stick = true; scrollBottom(true);

  const s = store.get("settings", {});
  const messages = [];
  if (s.sys) messages.push({ role: "system", content: s.sys });
  listState.current.messages.slice(0, -1).filter((m) => !(m.agent_run && !m.content)).forEach((m) => messages.push({ role: m.role, content: m.content, ...(m.attachments ? { attachments: m.attachments.map((a) => a.id) } : {}) }));
  const body = { messages, chat_id: chat.id, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, max_tokens: +s.max || DEFAULT_MAX_TOKENS, enable_thinking: thinking && modelThinks() };
  if (effort && modelThinks()) body.reasoning_effort = effort;
  if (s.temp) body.temperature = +s.temp;
  if (!chat.workspace && useFiles && sr?.running && !sr.error && !searchState.noFilesOnce) body.use_files = true;
  if (listState.current?.workspace) body.workspace = listState.current.workspace;
  if (listState.current?.workspace && permMode === "accept_edits") body.permission_mode = "accept_edits";
  searchState.noFilesOnce = false;
  listState.streaming = new AbortController(); updateSend(); renderHistory();

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
  const showPrompt = (prompt) => {
    const step = (bot.tool_steps || []).find((s) => s.id === prompt.id);
    if (step) step.status = "waiting";
    // Approval is interactive UI: paint it now, even when a previously queued
    // animation frame was suspended while the window was covered.
    if (raf) { raf < 0 ? clearTimeout(-raf) : cancelAnimationFrame(raf); }
    paint(); updateSend(); scrollBottom(true);
  };
  let streamFinished = false;
  try {
    for await (const evt of chatEvents(body, listState.streaming.signal, async (id) => {
      bot.stream_id = id; bot.stream_pending = true;
      if (!chat.deleted) await saveChat(chat);
    }, resumeBot?.stream_id)) {
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
            showPrompt(j.x_approval);
            continue;
          }
          if (j.x_question) {
            bot.pendingQuestion = j.x_question;
            showPrompt(j.x_question);
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
    streamFinished = true;
  } catch (e) {
    if (e.resumeExpired) bot.stream_pending = false;
    if (e.name === "AbortError" && actionRun) {
      try { await jpost(`/api/agent/runs/${actionRun}/stop`); } catch {}
    }
    bot.error = e.name === "AbortError" ? (bot.content ? "" : "Stopped.") : String(e.message || e);
  }
  if (bot.stream_id && (streamFinished || listState.streaming?.signal.aborted)) bot.stream_pending = false;
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
  if (savedReply && !streamFinished) {
    const { error, stream_pending } = bot;
    for (const key of Object.keys(bot)) delete bot[key];
    Object.assign(bot, savedReply, { error, stream_pending });
    // A failed replay cannot attach a partially replayed action to the restored reply.
    actionRun = null;
  }
  if (!bot.stream_pending) {
    for (const step of [...(bot.tool_steps || []), ...(bot.rounds || []).flatMap((r) => r.tools || [])]) {
      if (["running", "waiting"].includes(step.status)) {
        step.status = "error";
        step.error = bot.error || (listState.streaming?.signal.aborted ? "Stopped." : "The reply ended before this tool returned a result.");
      }
    }
  }
  listState.streaming = null; liveGen = null; updateSend(); renderHistory();
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
  if (listState.current === chat) updateCtx();
  if (!chat.deleted) await saveChat(chat);
  poll();
}

export {
  generate, liveGen, liveSkeletonHTML, send
};
