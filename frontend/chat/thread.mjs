// chat/thread.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { createStore } from "../core/store.mjs";
import { $, esc, icon, jpost, toast } from "../core/util.mjs";
import { agNoImages, md } from "../core/markdown.mjs";
import { chatHost, showView, view } from "../ui/views.mjs";
import { chLive, listState, renderHistory, saveChat, updateWorkspaceUI } from "./list.mjs";
import {
  activeChat, activeCodeWs, AG_RID, agLogKey, agWeb, attMeta, attThumb, autosize, cardHTML, chBind, chCardClick,
  chCardHTML, chShowSteps, codeUpdateStatus, DOC_ICON, generate, lib, liveGen, liveSkeletonHTML, loadAgent,
  loadCodeView, loadLibrary, patchCards, searchState, send, srHome, srOpen, updateCtx, updateSend
} from "../modules.mjs";

const CODE_STARTERS = [["i-book", "Explain this codebase", "Explain how this codebase is organized: the main modules, how they fit together, and where to start reading."], ["i-redo", "Fix failing tests", "Run the tests, find the first failure, and fix its root cause."], ["i-code", "Write tests for…", "Write tests for "], ["i-spark", "Add a feature…", "Add a feature that "]];
const STARTERS = [["i-code", "Write code", "Write a Python script that finds duplicate photos in a folder"], ["i-book", "Explain", "Explain how speculative decoding keeps output identical"], ["i-pencil2", "Draft", "Draft a polite follow-up email after a job interview"], ["i-spark", "Brainstorm", "Brainstorm names for a privacy-first note-taking app"]];
function renderThread() {
  updateWorkspaceUI();
  if (view === "code") codeUpdateStatus();
  const host = chatHost();
  const scroll = $("chatScroll"), wrap = $("composerWrap");
  let w = document.querySelector(".welcome");
  if (!listState.current || !listState.current.messages.length) {
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
    const keep = listState.chRun && listState.chRun.chat === listState.current ? listState.chRun.el : null;                     // the live card is the same element afterwards: typed answers and open panels stay
    $("thread").innerHTML = listState.current.messages.map((m, i) => turnHTML(m, i)).join("");
    if (listState.chRun && listState.chRun.chat === listState.current) {
      const ph = $("thread").querySelector(".agchat")?.closest(".turn");
      if (ph && keep) { keep.dataset.i = ph.dataset.i; ph.replaceWith(keep); } else if (ph) chBind(listState.chRun, ph);
    }
    if (liveGen && liveGen.chat === listState.current) liveGen.attach();
    $("input").placeholder = `Message ${activeChat()?.name || ""}`;
    const last = [...listState.current.messages].reverse().find((m) => m.stats);
    showPerf(last?.stats); drawSpark();
  }
  renderHistory(); updateSend(); updateCtx();
}
// First run: no chat model yet — recommend one that fits this Mac's memory.
const threadState = createStore({ sysRam: 0 });
function renderOnboarding(w) {
  $("chatScroll").style.display = "none";
  const wrap = $("composerWrap"); chatHost().appendChild(wrap); wrap.style.display = "none";
  const gb = threadState.sysRam / 2 ** 30;
  const picks = gb >= 40 ? ["qwen3.8-27b", "gemma-4-12b", "lfm2.5-1.2b"] : gb >= 24 ? ["gemma-4-12b", "qwen3-8b", "lfm2.5-1.2b"]
    : gb >= 16 ? ["gguf-qwen3.5-9b", "qwen3-8b", "lfm2.5-1.2b"] : ["lfm2.5-1.2b", "gguf-qwen3.5-9b"];
  const models = picks.map((id) => lib.chat.find((m) => m.id === id)).filter(Boolean);
  const grid = w.querySelector("#obCards");
  if (grid) { patchCards(grid, models.map((m, i) => cardHTML({ ...m, blurb: (i === 0 ? "Recommended for this Mac. " : "") + m.blurb })).join(""), models); return; }
  w.innerHTML = `<div style="max-width:880px;width:100%">
    <h1 style="margin-bottom:8px">Choose your first model</h1>
    <p style="text-align:center;color:var(--muted);margin:0 0 26px">Models run entirely on this Mac. Picked for your ${Math.round(gb)} GB of memory — you can add more any time in the Model library.</p>
    <div class="cards" id="obCards">${models.map((m, i) => cardHTML({ ...m, blurb: (i === 0 ? "Recommended for this Mac. " : "") + m.blurb })).join("")}</div>
    <p style="text-align:center;margin-top:18px"><button class="btn sm" data-view="library">Browse all models</button></p></div>`;
  w.onclick = async (e) => {
    const v = e.target.closest("button[data-view]"); if (v) { showView(v.dataset.view); return; }   // no inline handlers (CSP)
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
  if (step.status === "waiting") return "Waiting for your response…";
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
    const st = ["running", "waiting"].includes(step.status) ? "running" : step.status === "error" || step.error ? "error" : "done";
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
  if (listState.chRun && listState.chRun.msg === m) return chCardHTML(i);
  if (liveGen && liveGen.bot === m) return liveSkeletonHTML(i);
  const hasRounds = Array.isArray(m.rounds) && m.rounds.length > 0;
  const waiting = live && !m.content && !m.reasoning && !hasRounds && !(m.tool_steps?.length);
  const agentRid = typeof m.agent_run === "string" && AG_RID.test(m.agent_run) ? m.agent_run : "";
  const idle = m.agent_run && !m.content && !m.error;                                  // saved while the task was still running
  const body = idle ? `<span class="agidle">This task has no saved result yet.</span>` : m.switching ? `<span class="shimmer">${m.switching === "vision" ? `Loading ${esc(m.switchModel || "the vision engine")} with vision… (about 10 s)` : `Loading ${esc(m.switchModel || "the model")}… (first load takes 10–30 s)`}</span>` : waiting ? `<span class="shimmer">${(listState.current?.messages || []).some((x) => x.attachments?.length) ? "Reading attachments…" : "Thinking…"}</span>` : md(m.agent_run ? agNoImages(m.content) : m.content);
  const tail = live && m.content ? '<span class="caret"></span>' : "";
  const ckptHTML = !live && m.checkpoint?.turn_id
    ? (m.checkpoint.undone
        ? `<span class="undo-done">Turn reverted</span>`
        : `<button class="linkbtn undo-btn" data-undo-turn="${esc(m.checkpoint.turn_id)}" title="Revert all ${m.checkpoint.files?.length || 1} file edit(s) from this turn">Revert turn (${m.checkpoint.count || m.checkpoint.files?.length || 1})</button>`)
    : "";
  const actions = live ? "" : `<div class="actions"><button class="iconbtn" data-act="copy" title="Copy">${icon("i-copy")}</button>${m.agent_run ? "" : `<button class="iconbtn" data-act="regen" title="Regenerate">${icon("i-redo")}</button>`}${ckptHTML}${perfLine(m.stats)}</div>${perfGrid(m.stats)}`;
  const resume = m.stream_pending && m.stream_id && !live ? `<button class="btn sm" data-resume-stream>Resume reply</button>` : "";
  const agentTail = (agentRid ? `<div class="agtask"><button class="linkbtn" data-agview="${esc(agentRid)}">Task · view steps</button></div>` : "")
    + (m.computerSetup ? `<div class="agtask"><button class="btn sm" data-computer-setup="${m.computerSetup === "enable" ? "enable" : "settings"}">${m.computerSetup === "enable" ? "Enable computer access and continue" : "Computer access settings"}</button></div>` : "");
  const cut = !live && m.finish === "length" ? `<div class="cutoff">${icon("i-x", "width:14px;height:14px")}The reply hit the length limit. <button class="btn sm" data-act="continue">Continue</button></div>` : "";
  const stepsBlock = hasRounds
    ? `${chPlanHTML(m)}${chRoundsHTML(m)}${thoughtHTML(m, live)}`
    : `${thoughtHTML(m, live)}${chPlanHTML(m)}${chToolsHTML(m)}`;
  return `<div class="turn bot" data-i="${i}">${stepsBlock}${chHarnessCardHTML(m)}<div class="content">${body}${tail}</div>${resume}${agentTail}${live ? "" : sourcesHTML(m) + chWebSourcesHTML(m)}${m.error ? `<div class="err">${esc(m.error)}</div>` : ""}${cut}${actions}</div>`;
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
  const resume = e.target.closest("[data-resume-stream]");
  if (resume) {
    const message = listState.current?.messages?.[+resume.closest(".turn.bot").dataset.i];
    if (message) generate(message);
    return;
  }
  const full = e.target.closest(".uatts img[data-full]");
  if (full) { window.open(full.dataset.full, "_blank"); return; }
  const src = e.target.closest(".sources [data-open]");
  if (src) { srOpen(src); return; }
  const task = e.target.closest("[data-agview]");
  if (task) { const rid = task.dataset.agview; if (AG_RID.test(rid)) chShowSteps(task, rid); return; }
  const setup = e.target.closest("[data-computer-setup]");
  if (setup) {
    if (setup.dataset.computerSetup !== "enable") { showView("manage"); $("agSet").scrollIntoView({ block: "center" }); return; }
    const chat = listState.current, i = +setup.closest(".turn.bot").dataset.i, message = chat?.messages[i];
    setup.disabled = true;
    jpost("/api/agent/enable", { on: true }).then(() => {
      if (listState.current === chat && chat.messages[i] === message && message?.computerSetup && !listState.streaming && !chLive()) {
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
    const turnMsg = turnEl ? listState.current?.messages?.[+turnEl.dataset.i] : null;
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
      if (listState.current && !listState.streaming) saveChat(listState.current);
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
    const turnMsg = turnEl ? listState.current?.messages?.[+turnEl.dataset.i] : null;
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
      if (listState.current && !listState.streaming) saveChat(listState.current);
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
    const turnMsg = turnEl ? listState.current?.messages?.[+turnEl.dataset.i] : null;
    const feedback = decision === "deny" ? (card?.querySelector("[data-fb]")?.value || "").trim() : "";
    if (card) card.querySelectorAll("button, input").forEach((b) => b.disabled = true);
    try {
      await jpost(`/api/chat/sessions/${sid}/reply`, { decision, ...(feedback ? { feedback } : {}) });
      if (turnMsg) {
        const step = (turnMsg.tool_steps || []).find((s) => s.id === turnMsg.pendingApproval?.id);
        if (step?.status === "waiting") step.status = "running";
        delete turnMsg.pendingApproval;
      }
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
    const turnMsg = turnEl ? listState.current?.messages?.[+turnEl.dataset.i] : null;
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
    const turnMsg = turnEl ? listState.current?.messages?.[+turnEl.dataset.i] : null;
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
    const turnMsg = turnEl ? listState.current?.messages?.[+turnEl.dataset.i] : null;
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
  if (card) { if (listState.chRun?.el?.contains(card)) chCardClick(listState.chRun, e); return; }
  const turn = e.target.closest(".turn.bot"); if (!turn) return;
  if (e.target.closest("[data-perf]")) { turn.querySelector(".perfgrid")?.classList.toggle("on"); return; }
  const act = e.target.closest("[data-act]")?.dataset.act, m = listState.current.messages[+turn.dataset.i];
  if (act === "copy") { navigator.clipboard.writeText(m.content); toast("Copied to clipboard"); }
  if (act === "continue" && !listState.streaming) {
    searchState.noFilesOnce = true;                                    // "continue" is not a question to look up in the user's files
    $("input").value = "Continue exactly where you left off."; autosize(); send(); return;
  }
  if (act === "regen" && !listState.streaming && !chLive() && !m.agent_run) {
    listState.current.messages = listState.current.messages.slice(0, +turn.dataset.i); renderThread(); generate();
  }
};
function showPerf(x) {
  $("tDec").innerHTML = `${x?.decode_tokens_per_sec ?? "–"}<small>tok/s</small>`;
  $("tUp").innerHTML = `${x?.ceiling_tokens_per_sec ? (x.decode_tokens_per_sec / x.ceiling_tokens_per_sec).toFixed(2) : "–"}<small>×</small>`;
  $("tAcc").textContent = x?.accept_len ?? "–"; $("tTtft").innerHTML = `${x?.ttft_seconds ?? "–"}<small>s</small>`;
  const ctx = x?.context_tokens || 0; $("ctxTxt").textContent = `${ctx.toLocaleString()} tokens`; $("ctxBar").style.width = Math.min(100, ctx / listState.ctxWindow * 100) + "%";
}
function drawSpark() {
  const pts = (listState.current?.messages || []).filter((m) => m.stats?.decode_tokens_per_sec).slice(-20).map((m) => m.stats.decode_tokens_per_sec);
  const svg = $("spark"); if (pts.length < 2) { svg.innerHTML = ""; return; }
  const max = Math.max(...pts) * 1.1, W = 280, H = 44;
  const d = pts.map((v, i) => `${i ? "L" : "M"}${(i / (pts.length - 1) * W).toFixed(1)} ${(H - v / max * (H - 4) - 2).toFixed(1)}`).join(" ");
  svg.innerHTML = `<path d="${d} L${W} ${H} L0 ${H}Z" fill="currentColor" opacity=".07"/><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
}
// Auto-scroll only while the reader is at the bottom; scrolling up pauses it (a "↓" button returns).
let autoTop = -1; threadState.stick = true;                                                             // autoTop: where scrollBottom last put the view
const fmtSecs = (x) => { const n = Math.max(1, Math.round(x || 0)); return n === 1 ? "1 second" : n < 90 ? `${n} seconds` : `${Math.round(n / 60)} minutes`; };
function scrollBottom(force) {
  const s = $("chatScroll");
  if (force) threadState.stick = true;
  if (threadState.stick) { s.scrollTop = s.scrollHeight; autoTop = s.scrollTop; }
  $("jumpBtn").classList.toggle("on", !threadState.stick && s.scrollHeight - s.scrollTop - s.clientHeight > 80);
}
$("chatScroll").addEventListener("scroll", () => {
  const s = $("chatScroll"), gap = s.scrollHeight - s.scrollTop - s.clientHeight;
  // Our own scroll lands here too, sometimes after the reply grew or the composer area changed height: only a scroll
  // that moved the view away from where scrollBottom put it is the user's, and only that may stop following.
  if (threadState.stick && Math.abs(s.scrollTop - autoTop) < 2) return;
  threadState.stick = gap < 40;
  $("jumpBtn").classList.toggle("on", gap > 80);
}, { passive: true });
$("chatScroll").addEventListener("wheel", (e) => { if (e.deltaY < 0) threadState.stick = false; }, { passive: true });
$("jumpBtn").onclick = () => scrollBottom(true);
function modelThinks() { try { const a = activeChat(); return !a || a.thinking !== false; } catch { return true; } }   // lib may not exist yet at start-up

export {
  autoTop, chHarnessCardHTML, chPlanHTML, chRoundItemHTML, chRoundsHTML, chToolsHTML, chToolStepsInner,
  chWebSourcesHTML, CODE_STARTERS, diffLinesHTML, drawSpark, EDIT_TOOLS, fmtSecs, modelThinks, perfGrid, perfLine,
  PERM_WORDS, renderOnboarding, renderThread, scrollBottom, showPerf, sourcesHTML, STARTERS, thoughtHTML, threadState,
  TOOL_LABELS, toolLabel, toolResultHTML, turnHTML
};
