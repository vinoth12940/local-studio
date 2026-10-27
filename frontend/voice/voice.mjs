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
import { CHARACTERS, DEFAULT_CHARACTER } from "./characters.mjs";
import { endpointWait, isBackchannel, looksLikeOwnVoice, takeSentences } from "./turn.mjs";

const MIC_RATE = 16000;
const v = {
  on: false, state: "off", rms: 0, level: 0, es: null, sampler: 0,
  acceptInput: false, turnPending: false, gen: 0, speakId: 0, pending: new Map(), chain: Promise.resolve(),
  playedMs: 0, lastSpeechEnd: 0, respondAt: 0, firstAudioAt: 0, askedAt: 0,
};
const turn = { active: false, done: false, buf: "", held: [], roundSpoken: 0, chunks: 0, said: "", heard: "", toolActive: false,
  filler: false, workIdx: 0, lastSpoke: 0, tick: 0, interrupted: false, question: null };
const cap = { pre: [], chunks: [], on: false, voiced: 0, silence: 0, lastVoicedAt: 0 };
const pend = { text: "", timer: 0 };
let tent = null, loudSince = 0, tentBlockUntil = 0;
const WORKING = ["Let me check.", "Still looking.", "Reading a source now.", "Almost there."];

const character = () => CHARACTERS.find((c) => c.id === store.get("voiceCharacter", DEFAULT_CHARACTER)) || CHARACTERS[0];
const cleanSpeech = (s) => String(s || "").replace(/[*#`_~>|]/g, "").replace(/\[(.*?)\]\((.*?)\)/g, "$1").replace(/https?:\/\/\S+/g, "").replace(/\s+/g, " ").trim();
const post = (url, body) => fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });

// ---------------------------------------------------------------- the voice bar
function setState(s, note) {
  v.state = s;
  const bar = $("voiceBar"); if (!bar) return;
  bar.dataset.state = s;
  const label = { off: "Voice", starting: "Starting voice…", idle: "Listening", listening: "Listening", thinking: "Thinking…", speaking: character().name }[s] || s;
  $("vState").textContent = note || label;
}
function caption(text) { const el = $("vCap"); if (el) el.textContent = text || ""; }
function drawOrb() {
  const orb = $("vOrb"); if (!orb) return;
  const amp = v.state === "speaking" ? 0.35 + 0.25 * Math.random() : v.level;
  orb.style.transform = `scale(${(1 + Math.min(1, amp) * 0.9).toFixed(3)})`;
}

// ---------------------------------------------------------------- start and stop
async function startVoice() {
  if (v.on) return;
  v.on = true; $("voiceBar").hidden = false; setState("starting");
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
    setState("listening"); caption("Say something. Talk over me any time.");
    $("micBtn")?.classList.add("on");
  } catch (e) {
    toast(String(e.message || e)); await stopVoice();
  }
}
async function stopVoice() {
  const wasOn = v.on;
  v.on = false; v.acceptInput = false; v.gen++; clearInterval(v.sampler); clearInterval(turn.tick); clearTimeout(pend.timer);
  try { v.es?.close(); } catch {}
  v.es = null; v.pending.clear(); tent = null; turn.active = false; v.turnPending = false;
  if (wasOn) { await post("/api/voice/stop").catch(() => {}); await post("/api/voice/mic/close").catch(() => {}); }
  $("micBtn")?.classList.remove("on");
  if ($("voiceBar")) $("voiceBar").hidden = true;
  setState("off");
}

// ---------------------------------------------------------------- microphone events
function onEvent(ev) {
  if (ev.t === "mic") {
    v.rms = ev.rms;
    const u8 = Uint8Array.from(atob(ev.pcm), (c) => c.charCodeAt(0)), i16 = new Int16Array(u8.buffer), f = new Float32Array(i16.length);
    for (let k = 0; k < f.length; k++) f[k] = i16[k] / 32768;
    onMicChunk(f);
  } else if (ev.t === "played") {
    const p = v.pending.get(ev.id);
    if (p) { v.pending.delete(ev.id); v.playedMs += p.ms; turn.heard += " " + p.text; }
    if (!v.pending.size && turn.done) finishTurn();
  } else if (ev.t === "mic_restart") caption("The microphone stalled and was restarted.");
  else if (ev.t === "error") caption("The voice service stopped.");
}

// Every 30 ms: smooth the level, and decide about interruptions. With echo cancellation the assistant's own voice
// sits below the room noise, so a low bar; the canceller adapts during the first seconds, so the bar starts high.
function sample() {
  v.level += (Math.min(1, v.rms * 6) - v.level) * 0.35;
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
  if (!v.acceptInput) { cap.on = false; cap.chunks = []; cap.pre.push(chunk); if (cap.pre.length > maxPre) cap.pre.shift(); return; }
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
    if (!r.ok) return;
    const text = ((await r.json()).text || "").trim();
    if (!text || !v.on) return;
    if (tent && isBackchannel(text)) { endTent(); return; }      // "yeah", "uh-huh": keep talking
    if (performance.now() - v.lastSpeechEnd < 3000 && looksLikeOwnVoice(text, turn.said)) return;
    submitUtterance(text);
  } catch {}
}

// ---------------------------------------------------------------- turn taking
function submitUtterance(text) {
  clearTimeout(pend.timer);
  pend.text = (pend.text ? pend.text + " " : "") + text;
  const [wait] = endpointWait(pend.text);
  caption("You: " + pend.text);
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
    setState("thinking"); v.acceptInput = false; v.respondAt = performance.now();
    return;
  }
  if (listState.streaming) { caption("Still working on the last one. Say it again in a moment."); return; }
  v.acceptInput = false; v.turnPending = true; v.askedAt = performance.now(); v.respondAt = v.askedAt;
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
      // If its "played" notice is ever lost (the engine restarted), count it as played a little after it should have ended.
      setTimeout(() => { if (v.pending.has(id)) onEvent({ t: "played", id }); }, (seconds || 0) * 1000 + 2500 + v.pending.size * 400);
      if (v.state !== "speaking") { setState("speaking"); if (!v.firstAudioAt) v.firstAudioAt = performance.now(); }
    } catch { if (!v.pending.size && turn.done) finishTurn(); }
  });
}

// Called by chat/live.mjs: the extra request fields for a spoken turn ({} for a typed one).
function voiceRequest() {
  if (!v.turnPending) return {};
  v.turnPending = false;
  Object.assign(turn, { active: true, done: false, buf: "", held: [], roundSpoken: 0, chunks: 0, said: "", heard: "", toolActive: false,
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
    }
    return;
  }
  if (j.x_approval) { speak("I need your okay on screen for that."); return; }
  if (j.x_question) { turn.question = { session_id: j.x_question.session_id, id: j.x_question.id }; speak(j.x_question.question); return; }
  if (j.error) { speak("Sorry, something went wrong."); return; }
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
function renderCharacters() {
  const sel = $("vChar"); if (!sel) return;
  sel.innerHTML = CHARACTERS.map((c) => `<option value="${c.id}">${c.name}</option>`).join("");
  sel.value = character().id;
}
$("micBtn").onclick = () => (v.on ? stopVoice() : startVoice());
$("vEnd").onclick = () => stopVoice();
$("vChar").onchange = (e) => { store.set("voiceCharacter", e.target.value); };
renderCharacters();
// The mic button appears only when the voice.enabled flag is on.
(async () => {
  try {
    if (typeof fetch !== "function") return;
    const st = await (await fetch("/api/voice/status")).json();
    if (st.available) $("micBtn").hidden = false;
  } catch {}
})();

// As if you had said `text` (the dev server console and the checks use it through window.LS; the microphone is not needed).
function voiceHeard(text) { if (v.on) submitUtterance(String(text || "")); }
const voiceState = () => ({ on: v.on, state: v.state, acceptInput: v.acceptInput, pending: v.pending.size, speaking: turn.chunks, said: turn.said.trim() });

export { startVoice, stopVoice, voiceHeard, voiceOnEnd, voiceOnEvent, voiceRequest, voiceState };
