// voice/voice.mjs: talk to the assistant. The voice POC's conversation loop (build/voice-poc), inside Local Studio:
//  - the microphone and speaker are voice.py's echo-cancelling engine (Apple voice processing), relayed by /api/voice/*,
//    so the assistant's own voice never reaches what we listen to;
//  - what you say is transcribed on this Mac (Parakeet) and sent as a normal message in the current chat, so tools, web
//    search, approvals, memory and history are exactly those of typed chat (send(), generate());
//  - the reply is spoken sentence by sentence as it streams (Phonon), in the chosen character's voice;
//  - a sound from you first only PAUSES it; real speech (about 0.9 s) cuts it and stops the reply; a cough or "uh-huh"
//    lets it carry on; it waits when you sound mid-thought (turn.mjs).
import { $, store, toast } from "../core/util.mjs";
import { listState } from "../chat/list.mjs";
import { send } from "../chat/live.mjs";
import { showView, view } from "../ui/views.mjs";
import { CHARACTERS, DEFAULT_CHARACTER } from "./characters.mjs";
import { endpointWait, isBackchannel, looksLikeOwnVoice, takeSentences } from "./turn.mjs";

const MIC_RATE = 16000;
const v = {
  on: false, muted: false, state: "off", rms: 0, level: 0, micChunks: 0, es: null, sampler: 0,
  acceptInput: false, turnPending: false, gen: 0, speakId: 0, pending: new Map(), chain: Promise.resolve(),
  playedMs: 0, lastSpeechEnd: 0, respondAt: 0, firstAudioAt: 0, askedAt: 0,
};
const turn = { active: false, done: false, buf: "", held: [], roundSpoken: 0, chunks: 0, said: "", heard: "", heardParts: [], toolActive: false,
  filler: false, workIdx: 0, lastSpoke: 0, tick: 0, interrupted: false, question: null };
const cap = { pre: [], chunks: [], on: false, voiced: 0, silence: 0, lastVoicedAt: 0 };
const pend = { text: "", timer: 0 };
let tent = null, loudSince = 0, tentBlockUntil = 0;
const WORKING = ["Let me check.", "Still looking.", "Reading a source now.", "Almost there."];

const character = () => CHARACTERS.find((c) => c.id === store.get("voiceCharacter", DEFAULT_CHARACTER)) || CHARACTERS[0];
const cleanSpeech = (s) => String(s || "").replace(/[*#`_~>|\\]/g, "").replace(/\[(.*?)\]\((.*?)\)/g, "$1").replace(/https?:\/\/\S+/g, "").replace(/\s+/g, " ").trim();
const post = (url, body) => fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });

// ---------------------------------------------------------------- the Voice view and the small bar in chat
// The Voice view (#v-voice) is the main surface; while voice is on and another view is open, the small bar above the
// composer stands in for it.
function setState(s, note) {
  v.state = s;
  const shown = v.muted && s !== "off" && s !== "starting" ? "muted" : s;
  const label = { off: "Voice is off", starting: "Starting…", idle: "Listening", listening: "Listening", thinking: "Thinking", speaking: "Speaking", muted: "Muted" }[shown] || s;
  const bar = $("voiceBar");
  if (bar) { bar.dataset.state = s; $("vState").textContent = note || (shown === "speaking" ? character().name : label); }
  if ($("vv")) {
    $("vv").dataset.state = shown;
    $("vvState").textContent = note || label;
    const on = s !== "off";
    $("vvGo").className = "vv-btn " + (on ? "end" : "go");
    $("vvGo").setAttribute("aria-label", on ? "End conversation" : "Start talking");
    $("vvGoIcon").setAttribute("href", on ? "#i-x" : "#i-mic");
    $("vvGoTxt").textContent = on ? "End" : "Start";
  }
  syncBar();
}
function caption(text) {
  const el = $("vCap"); if (el) el.textContent = text || "";
  const n = $("vvNote"); if (n) { n.textContent = text || ""; n.classList.remove("err"); }
}
// Something failed: say what, in plain words, on screen and in the transcript, with the real reason (never a bare "went wrong").
function problem(text, detail) {
  const el = $("vCap"); if (el) el.textContent = text;
  const n = $("vvNote"); if (n) { n.textContent = text; n.classList.add("err"); }
  trAdd("step", detail ? `${text} (${detail})` : text)?.classList.add("err");
  console.warn("voice:", text, detail || "");
}
// The engine's error text, as the person would want to hear it.
function explainError(e) {
  const s = String(e || "");
  if (/not running|isn't running|no .* engine is ready|load .* first|connection attempts failed|switching/i.test(s)) return "No chat model is loaded. Load one in the sidebar.";
  if (/Engine answered 4\d\d/i.test(s)) return "The model refused the request.";
  if (/context|too long|max_tokens/i.test(s)) return "The conversation is too long for the model. Start a new chat.";
  if (/stopped/i.test(s)) return "";
  return "The reply failed.";
}
function asked(text) { const el = $("vvAsk"); if (el) el.textContent = text || ""; }
function saying(text) { const el = $("vvSay"); if (el) el.textContent = text || ""; }
function syncBar() { const bar = $("voiceBar"); if (bar) bar.hidden = !v.on || view === "voice"; }
// The display's bars: your level while listening, a speech-like movement while it speaks, a slow ripple while it thinks.
const BAR_SHAPE = [0.35, 0.55, 0.75, 0.9, 1, 0.9, 0.75, 0.55, 0.35];
function drawOrb() {
  const amp = v.muted ? 0 : v.state === "speaking" ? 0.45 + 0.45 * Math.random() : v.level;
  const orb = $("vOrb"); if (orb) orb.style.transform = `scale(${(1 + Math.min(1, amp) * 0.9).toFixed(3)})`;
  if (view !== "voice") return;
  const t = performance.now() / 1000, thinking = v.state === "thinking";
  document.querySelectorAll("#vvBars span").forEach((el, i) => {
    const a = thinking ? 0.18 + 0.12 * Math.sin(t * 5 + i * 0.7) : Math.max(0.08, Math.min(1, amp * BAR_SHAPE[i] * (0.7 + 0.6 * Math.random())));
    el.style.transform = `scaleY(${a.toFixed(3)})`;
  });
}
function restBars() { document.querySelectorAll("#vvBars span").forEach((el) => { el.style.transform = "scaleY(0.1)"; }); }

// The transcript beside the display: what you said, the tools it used, and what it said aloud.
const tr = { you: null, bot: null, count: 0 };
function trAdd(kind, text, who) {
  const log = $("vvLog"); if (!log) return null;
  if (!tr.count++) { log.innerHTML = ""; $("vvTime").textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
  const el = document.createElement(kind === "step" ? "p" : "div");
  if (kind === "step") { el.className = "vv-step"; el.textContent = text; }
  else { el.className = "vv-m " + kind; el.innerHTML = "<small></small><p></p>"; el.querySelector("small").textContent = who; el.querySelector("p").textContent = text; }
  log.appendChild(el);
  while (log.children.length > 80) log.firstChild.remove();
  log.scrollTop = log.scrollHeight;
  return el;
}
function trBot(text) {
  if (!text) return;
  if (!tr.bot) tr.bot = trAdd("bot", text, character().name);
  else { tr.bot.querySelector("p").textContent = text; const log = $("vvLog"); log.scrollTop = log.scrollHeight; }
}

// ---------------------------------------------------------------- start and stop
async function startVoice() {
  if (v.on) return;
  v.on = true; setState("starting"); saying(""); asked("");
  try {
    let st = await (await fetch("/api/voice/status")).json();
    if (!st.available) throw new Error("Voice is off. Turn on the voice.enabled flag in Settings first.");
    if (!st.running) {
      await post("/api/voice/enable");
      for (let i = 0; i < 600 && v.on; i++) {          // first time: installs the components and downloads the models
        await new Promise((r) => setTimeout(r, 1000));
        st = await (await fetch("/api/voice/status")).json();
        if (st.running && st.stt_loaded) break;
        if (st.setup?.status === "error") throw new Error(st.setup.error || "Voice could not start.");
        setState("starting", st.setup?.step || "Starting voice…");
      }
      if (!st.running) throw new Error("Voice didn't start. See logs/voice.log.");
    }
    if (!v.on) return;
    const r = await post("/api/voice/mic/open");
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "The microphone could not be opened.");
    v.es = new EventSource("/api/voice/events");
    v.es.onmessage = (e) => { try { onEvent(JSON.parse(e.data)); } catch {} };
    v.es.onerror = () => { if (v.on) caption("Reconnecting to the microphone…"); };
    v.playedMs = 0; v.acceptInput = true;
    clearInterval(v.sampler); v.sampler = setInterval(sample, 30);
    setState("listening"); caption("Say something. Talk over me any time."); saying("I'm listening.");
    selfCheck();
    $("micBtn")?.classList.add("on");
  } catch (e) {
    toast(String(e.message || e)); await stopVoice();
  }
}
// About a second, in the background, once listening: is sound arriving, is a model loaded, and can it speak and be
// understood (its own voice through the recognizer)? Anything wrong is shown as the part that failed, not as silence.
async function selfCheck() {
  const gen = v.gen; v.micChunks = 0;
  const failed = [];
  try {
    const h = await fetch("/api/llm/health");
    if (!h.ok) failed.push(["No chat model is loaded. Load one in the sidebar.", "model not ready"]);
  } catch {}
  try {
    const wav = await fetch("/api/voice/say", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ text: "Voice check, one two three.", voice: character().voice }) });
    if (!wav.ok) throw new Error("speech " + wav.status);
    const r = await fetch("/api/voice/transcribe", { method: "POST", headers: { "content-type": "audio/wav" }, body: await wav.blob() });
    const heard = r.ok ? ((await r.json()).text || "") : "";
    if (!/voice|check|one|two|three/i.test(heard)) failed.push(["Speech recognition isn't working.", r.ok ? `heard "${heard}"` : `HTTP ${r.status}`]);
  } catch (e) { failed.push(["The voice can't speak right now.", String(e.message || e)]); }
  await new Promise((res) => setTimeout(res, Math.max(0, 2500)));
  if (gen !== v.gen || !v.on) return;
  if (!v.micChunks) failed.push(["The microphone isn't sending any sound.", "check System Settings, Privacy & Security, Microphone"]);
  v.check = failed.length ? failed.map((f) => f[0]) : ["ok"];
  if (failed.length) failed.forEach(([t, d]) => problem(t, d));
}
async function stopVoice() {
  const wasOn = v.on;
  v.on = false; v.acceptInput = false; v.gen++; clearInterval(v.sampler); clearInterval(turn.tick); clearTimeout(pend.timer);
  try { v.es?.close(); } catch {}
  v.es = null; v.pending.clear(); tent = null; turn.active = false; v.turnPending = false;
  $("micBtn")?.classList.remove("on");
  v.muted = false; setMuteButton();
  setState("off"); restBars(); caption("");
  if ($("vvSay")?.textContent === "I'm listening.") saying("Press Start and just talk. You can talk over it any time.");
  if (wasOn) { await post("/api/voice/stop").catch(() => {}); await post("/api/voice/mic/close").catch(() => {}); }
}

// ---------------------------------------------------------------- microphone events
function onEvent(ev) {
  if (ev.t === "mic") {
    v.rms = ev.rms; v.micChunks++;
    const u8 = Uint8Array.from(atob(ev.pcm), (c) => c.charCodeAt(0)), i16 = new Int16Array(u8.buffer), f = new Float32Array(i16.length);
    for (let k = 0; k < f.length; k++) f[k] = i16[k] / 32768;
    onMicChunk(f);
  } else if (ev.t === "played") {
    const p = v.pending.get(ev.id);
    if (p) {                                             // in speaking order: a lost notice's stand-in can arrive early
      v.pending.delete(ev.id); v.playedMs += p.ms; turn.heardParts.push([ev.id, p.text]);
      turn.heard = turn.heardParts.sort((a, b) => a[0] - b[0]).map((x) => x[1]).join(" ");
    }
    if (!v.pending.size && turn.done) finishTurn();
  } else if (ev.t === "mic_restart") caption("The microphone stalled and was restarted.");
  else if (ev.t === "error") caption("The voice service stopped.");
}

// Every 30 ms: smooth the level, and decide about interruptions. With echo cancellation the assistant's own voice
// sits below the room noise, so a low bar; the canceller adapts during the first seconds, so the bar starts high.
function sample() {
  v.level += ((v.muted ? 0 : Math.min(1, v.rms * 6)) - v.level) * 0.35;
  drawOrb();
  const now = performance.now(), speaking = v.state === "speaking", thinking = v.state === "thinking" && now - v.respondAt > 700;
  const adapting = v.playedMs < 4000;
  const thr = adapting ? 0.6 : 0.15, dur = adapting ? 300 : 150;
  if ((speaking || thinking) && v.level > thr) {
    loudSince ||= now;
    if (!tent && now > tentBlockUntil && now - loudSince > dur) startTent();
    if (tent) tent.lastLoud = now;
  } else loudSince = 0;
  if (tent) judgeTent(now);
  if ((v.state === "idle" || v.state === "listening") && v.level > 0.12) userResumed();
}
function startTent() {
  const now = performance.now();
  tent = { t0: now, lastLoud: now, paused: v.state === "speaking" };
  if (tent.paused) post("/api/voice/pause").catch(() => {});
}
function endTent() {
  if (tent?.paused) post("/api/voice/resume").catch(() => {});
  tent = null; loudSince = 0; tentBlockUntil = performance.now() + 600;
}
function judgeTent(now) {
  if (v.state !== "speaking" && v.state !== "thinking") { endTent(); return; }
  const age = now - tent.t0, quiet = now - tent.lastLoud;
  if (age >= 900 && quiet < 300) interrupt();
  else if (quiet >= 450) endTent();                     // a cough or a door: carry on from where it paused
  else if (age > 4000) interrupt();
}

// Capture what you say: ~1.2 s of audio before you start is kept, so words said while it decides are not lost.
function onMicChunk(chunk) {
  const dur = chunk.length / MIC_RATE, voiced = v.level > 0.1, maxPre = Math.ceil(1.2 * MIC_RATE / chunk.length);
  if (!v.acceptInput || v.muted) { cap.on = false; cap.chunks = []; cap.pre.push(chunk); if (cap.pre.length > maxPre) cap.pre.shift(); return; }
  if (!cap.on) {
    cap.pre.push(chunk); if (cap.pre.length > maxPre) cap.pre.shift();
    if (voiced) { cap.on = true; cap.chunks = cap.pre.slice(); cap.voiced = 0; cap.silence = 0; setState("listening"); userResumed(); }
    return;
  }
  cap.chunks.push(chunk);
  if (voiced) { cap.voiced += dur; cap.silence = 0; cap.lastVoicedAt = performance.now(); } else cap.silence += dur;
  if (cap.silence > 0.5) {                               // short on purpose: endpointWait() decides whether you are finished
    const chunks = cap.chunks, voicedFor = cap.voiced; cap.on = false; cap.chunks = []; cap.pre = [];
    if (voicedFor > 0.3) transcribe(chunks);
  }
}
function wav16k(chunks) {
  let n = 0; for (const c of chunks) n += c.length;
  const buf = new ArrayBuffer(44 + n * 2), dv = new DataView(buf), w = (p, s) => [...s].forEach((ch, i) => dv.setUint8(p + i, ch.charCodeAt(0)));
  w(0, "RIFF"); dv.setUint32(4, 36 + n * 2, true); w(8, "WAVEfmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, MIC_RATE, true); dv.setUint32(28, MIC_RATE * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); w(36, "data"); dv.setUint32(40, n * 2, true);
  let o = 44; for (const c of chunks) for (let i = 0; i < c.length; i++, o += 2) dv.setInt16(o, Math.max(-1, Math.min(1, c[i])) * 32767, true);
  return new Blob([buf], { type: "audio/wav" });
}
async function transcribe(chunks) {
  try {
    const r = await fetch("/api/voice/transcribe", { method: "POST", headers: { "content-type": "audio/wav" }, body: wav16k(chunks) });
    if (!r.ok) { problem("I couldn't make out what you said.", (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`); return; }
    const text = ((await r.json()).text || "").trim();
    if (!text || !v.on) return;
    if (tent && isBackchannel(text)) { endTent(); return; }      // "yeah", "uh-huh": keep talking
    if (performance.now() - v.lastSpeechEnd < 3000 && looksLikeOwnVoice(text, turn.said)) return;
    submitUtterance(text);
  } catch (e) { problem("I couldn't make out what you said.", String(e.message || e)); }
}

// ---------------------------------------------------------------- turn taking
function submitUtterance(text) {
  clearTimeout(pend.timer);
  pend.text = (pend.text ? pend.text + " " : "") + text;
  const [wait] = endpointWait(pend.text);
  caption("You: " + pend.text); asked(pend.text);
  pend.timer = setTimeout(firePending, wait);
}
function userResumed() {                                 // you are still talking: hold the answer, give up after 2.5 s
  if (!pend.timer) return;
  clearTimeout(pend.timer); pend.timer = setTimeout(firePending, 2500);
}
async function firePending() {
  const text = pend.text; pend.text = ""; pend.timer = 0;
  if (!text || !v.on) return;
  if (turn.question) {                                   // the assistant asked you something: this is the answer
    const q = turn.question; turn.question = null;
    post(`/api/chat/sessions/${q.session_id}/reply`, { id: q.id, answer: text }).catch(() => {});
    trAdd("you", text, "You"); tr.bot = null;
    setState("thinking"); v.acceptInput = false; v.respondAt = performance.now();
    return;
  }
  if (listState.streaming) { caption("Still working on the last one. Say it again in a moment."); return; }
  v.acceptInput = false; v.turnPending = true; v.askedAt = performance.now(); v.respondAt = v.askedAt;
  trAdd("you", text, "You"); tr.bot = null; saying(""); caption("");
  setState("thinking");
  await send(text);
  if (v.turnPending) {                                    // send() refused (for example, another chat is still answering)
    v.turnPending = false; v.acceptInput = true; setState("listening"); caption("Couldn't send that right now.");
  }
}
function finishTurn() {
  clearInterval(turn.tick); turn.done = false;
  v.lastSpeechEnd = performance.now();
  if (!v.on) return;
  setState("idle");
  const gen = v.gen;
  setTimeout(() => { if (gen === v.gen && v.on) { v.acceptInput = true; setState("listening"); } }, 900);
}
// You talked over it: stop the speech and the reply, keep what was actually heard, listen.
function interrupt() {
  v.gen++; v.pending.clear(); clearInterval(turn.tick);
  post("/api/voice/stop").catch(() => {});
  tent = null; loudSince = 0; tentBlockUntil = performance.now() + 600;
  if (turn.active) { turn.interrupted = true; listState.streaming?.abort(); }
  turn.done = false; turn.active = false;
  v.lastSpeechEnd = performance.now(); v.acceptInput = true; setState("listening"); caption("Go ahead.");
  if (tr.bot && turn.heard.trim()) tr.bot.querySelector("p").textContent = turn.heard.trim() + " …";
  tr.bot = null;
}

// ---------------------------------------------------------------- speaking the reply
function speak(text) {
  const clean = cleanSpeech(text); if (!clean || !v.on) return;
  const gen = v.gen, id = ++v.speakId;
  turn.chunks++; turn.said += " " + clean; turn.lastSpoke = performance.now();
  v.chain = v.chain.then(async () => {
    if (gen !== v.gen) return;
    try {
      const r = await post("/api/voice/speak", { text: clean, voice: character().voice, id });
      if (!r.ok) throw new Error("speak " + r.status);
      const { seconds } = await r.json();
      if (gen !== v.gen) return;
      v.pending.set(id, { text: clean, ms: (seconds || 0) * 1000 });
      turn.shown = (turn.shown ? turn.shown + " " : "") + clean; saying(clean); trBot(turn.shown);   // the display: the sentence being said
      // If its "played" notice is ever lost (the engine restarted), count it as played a little after it should have ended.
      setTimeout(() => { if (v.pending.has(id)) onEvent({ t: "played", id }); }, (seconds || 0) * 1000 + 2500 + v.pending.size * 400);
      if (v.state !== "speaking") { setState("speaking"); if (!v.firstAudioAt) v.firstAudioAt = performance.now(); }
    } catch (e) {
      if (gen === v.gen) problem("I couldn't speak that.", String(e.message || e));
      if (!v.pending.size && turn.done) finishTurn();
    }
  });
}

// Called by chat/live.mjs: the extra request fields for a spoken turn ({} for a typed one).
function voiceRequest() {
  if (!v.turnPending) return {};
  v.turnPending = false;
  Object.assign(turn, { active: true, done: false, buf: "", held: [], roundSpoken: 0, chunks: 0, said: "", heard: "", heardParts: [], shown: "", toolActive: false,
    filler: false, workIdx: 0, lastSpoke: performance.now(), interrupted: false });
  clearInterval(turn.tick);
  const gen = v.gen;
  turn.tick = setInterval(() => {                       // long tool work: say something now and then instead of going silent
    if (gen !== v.gen || !turn.active) return clearInterval(turn.tick);
    if (turn.toolActive && performance.now() - turn.lastSpoke > 6500 && turn.workIdx < WORKING.length) speak(WORKING[turn.workIdx++]);
  }, 500);
  return { voice: true, voice_style: character().style, enable_thinking: false };
}
// Called by chat/live.mjs for every streamed event of the current reply.
// A model sometimes says what it is about to do before calling a tool ("I'll search the web…"); that round ends in tool
// calls and must not be spoken. So a round's first sentence is held until a second one arrives or the round ends in
// "stop" (one sentence of delay at most), and anything held is dropped when the round turns out to be a tool call.
function flushHeld() { turn.held.splice(0).forEach((x) => { turn.roundSpoken++; speak(x); }); }
function dropRound() { turn.held = []; turn.buf = ""; turn.roundSpoken = 0; }
function voiceOnEvent(j) {
  if (!turn.active) return;
  if (j.x_tool) {
    turn.toolActive = j.x_tool.status === "running";
    if (turn.toolActive) {
      dropRound();                                       // words before a tool call are the model thinking aloud
      if (!turn.filler && !turn.chunks) { turn.filler = true; speak("Let me check."); }
      caption(j.x_tool.summary || j.x_tool.name);
      trAdd("step", j.x_tool.summary || j.x_tool.name);
    }
    return;
  }
  if (j.x_approval) { speak("I need your okay on screen for that."); return; }
  if (j.x_question) { turn.question = { session_id: j.x_question.session_id, id: j.x_question.id }; speak(j.x_question.question); return; }
  if (j.error) {
    const said = explainError(j.error);
    if (said) { speak(said); problem(said, String(j.error).slice(0, 240)); }
    return;
  }
  const ch = j.choices?.[0], d = ch?.delta?.content;
  if (d) {
    turn.toolActive = false; turn.buf += d;
    let sents; [sents, turn.buf] = takeSentences(turn.buf, false, turn.chunks === 0 && !turn.held.length);
    turn.held.push(...sents);
    if (turn.held.length >= 2 || (turn.roundSpoken && turn.held.length)) flushHeld();
  }
  if (ch?.finish_reason === "tool_calls") dropRound();
  else if (ch?.finish_reason) { turn.held.push(...takeSentences(turn.buf, true)[0]); turn.buf = ""; flushHeld(); }
}
// Called by chat/live.mjs when the reply stream ends (finished, failed or stopped).
function voiceOnEnd(bot) {
  if (!turn.active && !turn.interrupted) return;
  if (turn.interrupted) {                                // keep in the chat only what was actually said aloud
    if (bot && turn.heard.trim()) bot.content = turn.heard.trim() + " …";
    turn.interrupted = false; return;
  }
  turn.held.push(...takeSentences(turn.buf, true)[0]); turn.buf = ""; flushHeld();
  turn.active = false; turn.done = true;
  v.chain.then(() => { if (!v.pending.size && turn.done) finishTurn(); });
  if (turn.question) { turn.done = false; v.acceptInput = true; }   // waiting for your spoken answer
}

// ---------------------------------------------------------------- controls
const CHECK = '<svg class="i"><use href="#i-check"/></svg>';
function renderCharacters() {
  const c = character(), sel = $("vChar");
  if (sel) { sel.innerHTML = CHARACTERS.map((x) => `<option value="${x.id}">${x.name}</option>`).join(""); sel.value = c.id; }
  const list = $("vvChars");
  if (list) list.innerHTML = CHARACTERS.map((x) => `<button class="vv-ch${x.id === c.id ? " on" : ""}" data-ch="${x.id}" aria-pressed="${x.id === c.id}">`
    + `<i style="background:${x.color}"></i><span><b>${x.name}</b><small>${x.tag}</small></span>${CHECK}</button>`).join("");
  $("vv")?.style.setProperty?.("--vc", c.color);
  if ($("vvName")) $("vvName").textContent = c.name;
}
function pickCharacter(id) { store.set("voiceCharacter", id); renderCharacters(); }
function setMuteButton() {
  $("vvMute")?.classList.toggle("on", v.muted);
  $("vvMuteIcon")?.setAttribute("href", v.muted ? "#i-micoff" : "#i-mic");
  if ($("vvMuteTxt")) $("vvMuteTxt").textContent = v.muted ? "Unmute" : "Mute";
  $("vvMute")?.setAttribute("aria-label", v.muted ? "Unmute" : "Mute");
}
function toggleMute() {                                  // muted: it keeps talking, but hears nothing and can't be cut off
  if (!v.on) return;
  v.muted = !v.muted; setMuteButton(); setState(v.state);
}
// Called by ui/views.mjs whenever the view changes: opening Voice starts it, and the small bar shows elsewhere.
function voiceViewShown(name) {
  syncBar();
  if (name !== "voice") return;
  renderCharacters();
  if (!v.on) startVoice();
}
$("micBtn").onclick = () => { showView("voice"); };
$("vEnd").onclick = () => stopVoice();
$("vChar").onchange = (e) => pickCharacter(e.target.value);
$("vvGo").onclick = () => (v.on ? stopVoice() : startVoice());
$("vvMute").onclick = toggleMute;
$("vvType").onclick = () => { showView("chat"); };
$("vvChars").onclick = (e) => { const b = e.target.closest?.("[data-ch]"); if (b) pickCharacter(b.dataset.ch); };
renderCharacters();
// The mic button and the Voice item appear only when the voice.enabled flag is on.
(async () => {
  try {
    if (typeof fetch !== "function") return;
    const st = await (await fetch("/api/voice/status")).json();
    if (st.available) { $("micBtn").hidden = false; $("navVoice").hidden = false; }
  } catch {}
})();

// As if you had said `text` (the dev server console and the checks use it through window.LS; the microphone is not needed).
function voiceHeard(text) { if (v.on) submitUtterance(String(text || "")); }
const voiceState = () => ({ on: v.on, muted: v.muted, state: v.state, check: v.check, micChunks: v.micChunks, acceptInput: v.acceptInput, pending: v.pending.size, speaking: turn.chunks, said: turn.said.trim() });

export { startVoice, stopVoice, voiceHeard, voiceOnEnd, voiceOnEvent, voiceRequest, voiceState, voiceViewShown };
