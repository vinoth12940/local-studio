# Voice agent ("Jarvis") — findings and final plan

Status: the standalone proof of concept in `build/voice-poc` (page v23) is finished and was used to find the hard
problems below. Nothing is built into Local Studio yet. Everything in this plan lands behind flags that default to off.

Goal: talk to Local Studio like a person — interrupt it, pause mid-thought, ask about today's news or your own files,
ask it to act — with every byte of audio and every model on the Mac, and with the same safety rules as typed chat.

## 1. What the POC proved (measured, not guessed)

| Works | Evidence |
|---|---|
| Streaming reply, spoken sentence by sentence | first audio 0.6 s median on small talk (Self-test) |
| Local listening and speaking | Parakeet transcribed perfectly in 66 ms warm; Kokoro 0.08 s per sentence |
| Agent tools by voice through the real harness | live Bitcoin price and Tokyo weather with sources |
| Turn detection, pause-first interruption, backchannels | Self-test: audio stops in 0.17 s, 0/4 coughs cut, 4/4 "yeah" ignored, 4/4 "stop" cut |
| Memory that survives reloads and knows where you cut in | Self-test and manual checks |

### The three problems you reported, with root causes

**1. "It thinks I'm interrupting while it speaks."** The interruption detector trusts Chrome speech recognition while
the assistant is talking. Through speakers, the recognizer hears the assistant's own voice and mis-transcribes it
("79,700" for "seventy nine thousand", "coin market cap", "because" for "bitcoin"). The own-voice filter matches words,
so **6 of 10 realistic mis-hearings pass as you** and cut it instantly, skipping the pause-first step. The loudness
detector adds occasional pauses (1–2 per 4 s of strong echo in the Self-test). Root cause: there is no real acoustic
echo cancellation with the assistant's audio as the reference. Browser echo cancellation is designed around calls and is
not guaranteed to cancel audio the page plays through Web Audio; no word filter can fix acoustics.

**2. "Cricket news is pulled from old pages and hallucinated."** Running the real tool code on "latest cricket news":
- `web_search` scrapes DuckDuckGo's HTML page: 6 titles and snippets, **no dates, no recency filter**; for news it returns
  only index pages whose snippets are SEO text ("Cricket News – Latest and Breaking…"), not news.
- `read_webpage` identifies as `LocalStudio/1.4.2` and runs no JavaScript: **ESPNcricinfo and NDTV return HTTP 403**;
  Cricbuzz loads but gives undated headlines mixed with menus and duplicates.
- The model (LFM2.5-24B-A2B, about 2B active parameters) gets almost no dated evidence, makes 6 tool calls in 23 s
  despite "one search", and answered "news" with *fixtures* from an unknown aggregator (sports360news.com).
- The same tool reads **dated** feeds fine: Google News RSS (`when:1d`) and ESPNcricinfo RSS returned real headlines
  timestamped today — but it returns raw XML cut at 6,000 characters (3–5 items) because nothing parses feeds.

**3. "It doesn't talk like a person."** (a) every sentence is synthesized separately, so intonation resets and there is
a gap at each boundary; (b) the persona forces a reaction first, so replies repeat "Oh nice!" / "Ha, fair question!";
(c) Kokoro is fast but flat, Chatterbox livelier but 1.5–3.7 s per sentence with gaps; (d) web turns take 10–25 s;
(e) self-interruptions and echo pauses break the flow; (f) the small model answers oddly at times
("I can't say that… your code word is pineapple, but I don't repeat them back").

### Also found
- **Security hole in the POC proxy, fixed:** `serve.py` dropped the `Origin` header when forwarding to the harness,
  bypassing Local Studio's local-only guard; a website could have sent a cross-site "simple" POST that drives read-only
  tools (auto-approved on the dev server) and leaks data through a web request. Now refused: foreign Origin,
  cross-site fetch, non-loopback Host (DNS rebinding), non-JSON POST. All five attack shapes return 403.
- The mlx-audio server (:8000) answers cross-origin preflights. It only does speech in/out, but the product needs the
  same guard and a token.
- Chrome speech recognition sends audio to Google. That breaks the product promise and cannot ship.
- Routing between "answer directly" and "search" is a rule-plus-classifier hack: on labeled questions 2/30 changing
  facts went to the fast path (both among the 7 written after the rules), 5/21 timeless ones searched needlessly.

## 1b. Prior art (checked 2026-10-27) and what it changes

People are building this exact stack on Apple Silicon, and it converges on the same pieces this plan picked:

| Project | Stack | Relevance |
|---|---|---|
| [macvoice](https://github.com/johnrobinsn/macvoice) (Apache-2.0, very new, 0 stars) | Silero VAD → Smart Turn v3.2 → Parakeet (MLX) → any OpenAI-compatible LLM → Phonon 31M | echo cancellation from **Python** via PyObjC + one AVAudioEngine with voice processing: agent voice at the mic −21 → −78 dBFS on M5 speakers (its own numbers); 250 ms of speech to barge in, first 0.5 s ignored; 1.4 GB pipeline; ~0.77 s to first audio with Qwen3-4B |
| [voice-stack](https://github.com/Ianodad/voice-stack) | Pipecat + parakeet-mlx + Qwen (mlx-lm) + Kokoro | no AEC locally: barge-in needs headphones (our POC's problem) |
| [local-voice](https://github.com/mthomas100/local-voice) | Pipecat, Smart Turn v3, streaming ASR, Qwen3-TTS | pause-then-interrupt like ours; numbers synthetic, early |
| [Kyutai Unmute](https://github.com/kyutai-labs/unmute) | streaming STT + semantic VAD + streaming TTS around any text LLM | sub-second; GPU-oriented, not Mac-first |

**Phonon (Gradium), open-sourced 2026-10-20:**
- `gradium/phonon-31m-en` is English, **Apache-2.0**, ungated, 64 MB q8, 37 voices, streams in 80 ms frames. Gradium claims 7 ms to first audio on an M5 CPU and higher naturalness and lower WER than Kokoro. Those are Gradium's own tests; a third-party write-up says its spec sheets disagree.
- `phonon-40m` (per-language and multilingual EN/FR/DE/ES/PT) is **CC BY-NC-4.0** and gated, so it is not shippable in a commercial product without a license.
- Runtime: `ptts` on PyPI (Gradium, MIT/Apache, Rust + Metal, no PyTorch; 0.5.0 released 2026-10-27). Also MLX-Audio, Ollama and LM Studio. **`phonon` on PyPI is an unrelated 2020 package**: never install that name.
- Voice cloning goes through Gradium Studio (a hosted service). A cloned voice means uploading a recording, which is outside the "all on the Mac" rule, so built-in voices only, by default.

**Phonon spike in the POC (2026-10-27, this Mac):** all 37 English voices generated; warm first audio **6 ms**, about
**30× faster than real time**, so Gradium's speed claim holds here (end-to-end through the page server: 0.06–0.11 s per
sentence). Its built-in normalizer reads money and numbers correctly ("$79,785" → "79 thousand 785 dollars").
`voices.html` auditions every voice; 13 **characters** (`characters.json`: voice + speaking style) are selectable in the
assistant. Lessons: a character must never limit helpfulness (the pirate first refused a cooking question; fixed with an
explicit rule); a small model copies earlier replies' style after a switch, which prompting did not stop, so only the
current character's own replies are sent back (user messages always kept). End-to-end first audio is about 1 s, of which
about 0.5 s is the 24B chat model reaching its first speakable clause.

**Echo-cancellation spike (2026-10-27, this Mac, built-in speakers):** `build/voice-poc/audio_bridge.py` runs mic and
speaker through one AVAudioEngine with voice processing (PyObjC). Measured with `aec_check.py`: the assistant's voice at
the mic went from −27.0 to −60.8 dBFS median (−14.8 → −46.7 loudest 5%), about 34 dB, ending 12 dB *below* the room
noise. Live through the page, with nobody talking: 0 self-cuts across about 90 s of speech in three runs. The only
leak is in the first ~250 ms of a session's first reply while the canceller adapts (a 0.37 spike), handled by a higher
interrupt bar for the first 4 s of playback; after that, 0 pauses and 0 false alarms. Not yet verified: interrupting
with a real voice through this path, music or TV in the room, Bluetooth headsets.

Changes to this plan: (1) echo cancellation can live in the Python voice service via PyObjC first (Swift later only if
needed); (2) default English TTS candidate becomes Phonon 31M, to be verified by ear and measured against Kokoro in
Phase 3; (3) reuse or adapt macvoice's Apache-2.0 loop (Smart Turn + AEC wiring) instead of re-inventing it, keeping
Local Studio's harness as the brain.

## 2. Target architecture

```
 Mac app (Swift)                     Voice service (Python, :8084)            Local Studio backend (:7860)
┌───────────────────────────┐  WS   ┌─────────────────────────────┐   WS   ┌───────────────────────────────┐
│ AVAudioEngine             │◄─────►│ Silero VAD on clean audio   │◄──────►│ services/voice.py orchestrator│
│  voice processing IO:     │ PCM   │ streaming STT (Parakeet RT /│ events │  state machine, barge-in      │
│  AEC + noise + AGC, with  │ 16 kHz│  Nemotron streaming)        │        │  policy, turn manager, router │
│  the TTS output as the    │       │ end-of-turn model + rules   │        │  context = what was heard     │
│  echo reference           │       │ TTS (Kokoro / chosen voice) │        │  → /api/chat harness (tools,  │
│ mic indicator, push-to-   │       │  streamed audio chunks      │        │    approvals, memory, gate)   │
│ talk, wake word (later)   │       └─────────────────────────────┘        └───────────────────────────────┘
└───────────────────────────┘
```

Decisions and why:

1. **Audio in the native layer, not the browser.** macOS voice-processing I/O (Apple's built-in echo canceller for
   call-style audio, `AVAudioEngine` with voice processing enabled) cancels the app's own output at the source, so the mic signal contains you and not the assistant. This one change removes the
   self-interruption class of bugs. The browser POC stays as a dev tool, with Headphones mode recommended. A spike
   should also try routing TTS through a WebRTC loopback so Chrome's call AEC gets a reference; whether that works must
   be measured, not assumed.
2. **No cloud speech.** Local streaming STT replaces Chrome recognition. Interruption decisions use VAD on the
   echo-cancelled signal plus local transcripts, never raw loudness against speaker echo.
3. **Voice service is its own process**, like `search.py`: holds the speech models (~1–3 GB), restartable, exempt from
   the one-engine rule, launched only by the backend with fixed command patterns kept in sync with `stop.sh`, deps
   pinned in `voice-requirements.txt`, installed only when the user turns voice on.
4. **One brain.** Every turn goes through the existing `/api/chat` harness (tools, approvals, memory, verification
   gate); the orchestrator adds voice behavior, never a second agent. The "direct vs search" decision moves out of the
   page: the model decides with tools available, and the grounding rules below make that safe.
5. **Grounded answers or honest refusals** (section 3) — accuracy over speed for anything factual.

### Turn-taking and interruption policy (the part that makes it feel human)

- End of turn = VAD silence + an end-of-turn model (Smart Turn-class, open, ONNX) + the POC's rules as a fallback.
  Measured target: answer within 0.4 s after a finished sentence; hold up to 1.6 s after "…and", "…my".
- While it speaks: a voice onset on the **echo-cancelled** signal pauses audio at once (target < 200 ms). Committing the
  cut requires local transcript words that are not a backchannel, or ~700 ms of continuous voice. Coughs, "uh-huh",
  "yeah" resume from the same word. Context keeps only what was actually heard (already in the POC).
- While it searches or thinks: speech cancels the turn and its tool calls.
- Streaming TTS with sentence-overlap context so intonation carries across sentences; varied acknowledgements instead
  of a fixed "react first"; short spoken progress during tools ("Checking the latest from ESPNcricinfo…").

## 3. Grounding: making "latest news" true

1. **New read-only tool `news_search(query, since)`** returning structured items `{title, source, published_at, url}`
   from dated feeds (Google News RSS with a time window, plus publisher feeds such as ESPNcricinfo), parsed properly,
   deduplicated, sorted by time, filtered to the window. Same public-IP pinning and size limits as `read_webpage`.
2. **`web_search` returns dates when available** and accepts a recency window; `read_webpage` parses RSS/Atom and
   extracts the main article text (readability-style) instead of the first 6,000 characters of menus.
3. **Source honesty:** keep the honest user agent. A site that refuses (403) is reported as refused, never worked
   around by impersonating a browser; the model is told which sources failed.
4. **Answer rules for voice (enforced, not just prompted):** a news/price/score answer must name its source and how
   old it is ("ESPNcricinfo, two hours ago"); if no item is newer than the window, say so ("I couldn't find anything
   from today"). A new grounding check (new work; the existing verification gate covers coding tasks, not facts)
   verifies that names, numbers and dates in the answer appear in the tool output before it is spoken.
5. **Model choice by measurement:** run the same grounding eval (news, prices, sports, "who currently…") on the
   installed models (LFM2.5-24B-A2B, Qwen3.6-35B-A3B, Qwen3.8-27B) using `macos/evals`; pick the default voice model by
   hallucination rate first, latency second.

## 4. Security and privacy model

| Threat | Control |
|---|---|
| Another website drives the agent through a local port | every voice route behind `LocalOnly`; WS needs a per-launch token from the app; no proxy that drops headers (POC hole fixed) |
| A TV, video or another person speaks a command | push-to-talk by default; wake word optional; actions keep the existing approval gate; approvals are clicked on screen, never by voice, by default |
| Web page text tells the model to do something (prompt injection) | web content stays marked untrusted; during voice turns only read-only tools run without a click; no automatic chaining that sends local file content into a web request |
| Audio leaves the Mac | local STT/TTS only; Chrome recognition removed from the product |
| Audio kept on disk | never written; only text transcripts in chat history; debug dumps off and hidden |
| Eavesdropping without the user knowing | always-visible mic state, one-key mute, mic released when idle |
| Voice service abused or crashed | own process, bounded queues, request size limits, idle model unload, health checks, automatic restart |

## 5. Edge cases the design must handle (each becomes a test)

Speaker echo at high volume; music or TV in the room; two people talking; you talk while it starts speaking; long
pauses mid-thought; "um/uh"; backchannels; coughs, door slams; interruption during a tool call, during an approval,
during the first 0.5 s of speech; numbers, units, currencies and dates read aloud correctly; names and accents;
Bluetooth headsets (AirPods switch to a low-quality call profile when the mic opens — detect and warn); audio device
change mid-call; Mac sleep/wake; mic permission denied or revoked; chat engine not loaded, switching or out of memory
(say so, never hang); search provider down or blocked (say so); slow model (spoken progress, cancellable); very long
answers (offer to put details on screen); long sessions (context compaction); typing in the chat UI while voice is
active (one conversation, no double replies); app quit mid-sentence; memory pressure from engine + speech models
(the POC hit 47 GB and stalled the voice server — measure and unload).

## 6. Plan

| Phase | Deliverable | Done when |
|---|---|---|
| **0. Evidence harness** | record a 30–60 min corpus of your real voice in your room (speakers and headphones): questions, interruptions, backchannels, pauses, coughs, TV in background; scripts that replay it through the pipeline and score it | baseline numbers for the POC on real audio, not synthetic tones |
| **1. Grounding fixes** (backend, flag `harness.news_search`) | `news_search`, dated `web_search`, RSS + main-text extraction in `read_webpage`, source/age rule, refusal when stale; grounding eval across installed models | news/price/sports eval: ≥ 95% answers cite a source dated inside the window, 0 fabricated items on the eval set |
| **2. Native audio + voice service** (flag `voice.enabled`) | Swift voice-processing capture/playback in `main.swift`, voice service with VAD + streaming STT + TTS, `/api/voice` WS with token, orchestrator state machine, mic permission + entitlement | on the corpus over speakers: 0 self-interruptions in 10 min of assistant speech; barge-in pause < 200 ms; commit < 800 ms |
| **3. Natural conversation** | end-of-turn model, streamed TTS with prosody carry-over, voice bake-off on a quiet machine (memory-safe, one model at a time), persona rewrite, spoken progress | blind A/B on your recordings prefers it to the POC; first audio < 1 s on small talk |
| **4. Agent actions by voice** | file/doc questions, coding status, computer-use hand-off, all through existing approvals (on-screen) | every action path covered by a voice test; no action runs without the same approval typed chat needs |
| **5. Hands-free** (optional) | wake word, always-listening mode with visible indicator | false-wake rate measured on the corpus with TV playing |

Each phase follows the repo's rules: new routes and SSE events update the contract fixtures, new runtime files go into
`macos/build.sh`, Python deps are pinned, nothing is tested against the running app, and `macos/build.sh` passes.

## 6b. Integration into Local Studio (concrete)

The POC page did everything in the browser. In the product the real-time loop moves next to the audio, and the voice
service becomes **another client of `/api/chat`**, exactly like the POC page was. One brain, one set of tools and approvals.

```
 UI (frontend/voice/voice.mjs)      backend (studio/)                       voice.py process (:8084, own deps)
 mic button, captions, orb  ──SSE── api/voice.py  /api/voice/*  ──token──  audio bridge (AVAudioEngine + voice processing)
 Settings → Voice (characters)      services/voice.py launcher              Silero VAD + Smart Turn + endpoint rules
                                    harness: voice mode, news_search        Parakeet STT · Phonon 31M TTS
                                    /api/chat  ◄──────── voice turns ─────── barge-in / backchannel / pause-first logic
```

| Area | Change | Pattern it follows |
|---|---|---|
| `voice.py` + `voice/` package + `voice-requirements.txt` | the service: bridge, VAD/turn, STT, TTS, loop; calls `/api/chat` with `voice: true` | `search.py` + `search-requirements.txt` (pinned; installed by `setup.sh --voice` only when the user turns voice on) |
| `studio/config.py` | `VOICE_PORT` 8084 (`LOCAL_STUDIO_VOICE_PORT` override for dev, e.g. 8094) | `SEARCH_PORT` |
| `studio/services/voice.py` | `_start_voice()` / `_stop_voice()`, health, per-launch token in env | `services/search.py` (`cwd=ROOT`, relative file names, `uv run --with-requirements`) |
| `stop.sh` | `pkill` patterns for `python voice.py --port 8084` | the search patterns on lines 15–16; keep in sync |
| `studio/api/voice.py` | `GET /api/voice/status`, `POST /api/voice/start`, `/stop`, `/ptt`, `GET /api/voice/events` (SSE: state, captions, levels, route, errors), `POST /api/voice/settings` | one `APIRouter` per area; `routes.json` and `sse_contract.json` fixtures updated in the same commit |
| harness (`api/chat.py`, `services/harness.py`, `services/web.py`) | `voice: true` mode: spoken-style note, no thinking, fewer tool rounds, approvals to the UI only; `news_search` tool, dated `web_search`, RSS/main-text in `read_webpage`; source-and-age rule | existing flags pattern: `harness.news_search`, `voice.enabled`, `voice.hands_free` (all false) |
| chats | voice turns saved as normal messages (`via: "voice"`) in the current chat, so typing and talking share one conversation | `data/chats/<id>.json` |
| UI | `frontend/voice/voice.mjs` (added to `frontend/modules.mjs` in order), mic button in the composer, live captions in the thread, mic-on indicator; `frontend/settings/voice.mjs` with the 13 characters | module order and store rules in CLAUDE.md; `check_ui.js` must pass |
| Mac app | `NSMicrophoneUsageDescription` in the Info.plist `build.sh` writes (missing today); audio-input entitlement if the signing uses the hardened runtime; clear message if permission is denied | `macos/build.sh` lines 60–71 |
| build | add `voice.py voice-requirements.txt` (and the `voice/` package, `characters.json`) to the copy list on `build.sh` line 44 | same list as `search.py` |
| checks | `macos/check_voice.py`: offline, synthetic audio: endpointing, backchannels, pause-first barge-in, routing, security of the voice port (token, Origin, Host); plus the real-voice corpus eval | `check_search.py`, `eval.mjs`, the POC Self-test scenarios |

Reuse from the POC: `endpointWait`, `isBackchannel`, the pause-first interruption policy, character rules and
`ctxHistory` (port to Python with their labeled test sets); `audio_bridge.py` and `serve.py`'s local-only checks;
macvoice's Apache-2.0 modules for Silero VAD and Smart Turn (with NOTICE attribution).

Licences to confirm before shipping: Phonon 31M Apache-2.0 (checked), Parakeet TDT 0.6B v3 (NVIDIA; check its
attribution terms), Silero VAD, Smart Turn, macvoice (Apache-2.0, checked).

Suggested pull requests, each behind its flag and passing `macos/build.sh`:
1. **Grounding** (`harness.news_search`): news tool, dated results, RSS, source/age rule, grounding eval. Independent of
   audio and useful for typed chat too.
2. **Voice service skeleton** (`voice.enabled`): `voice.py` with bridge + Phonon + Parakeet, launcher, stop patterns,
   `setup.sh --voice`, mic permission, build list, `check_voice.py` basics. **Built on branch `voice/service-skeleton`.**
   Verified on an isolated dev server (port 7871, own state, voice port 8094): enable installs the 56 pinned packages and
   starts the service with the mic closed; `/api/voice/say` 0.18 s, `/api/voice/transcribe` 0.19 s with an exact round
   trip; the service's echo-cancelling engine streamed mic chunks, played a sentence and closed; disable leaves no process.
   Open: while the assistant speaks, voice processing reads −100 dBFS at the mic (quieter than the room), so whether a
   real voice talking over it still gets through must be tested with a person before the conversation loop (PR 3)
   relies on it. Backend routes for the microphone and the event stream come with PR 3.
3. **Conversation loop**: VAD, Smart Turn, barge-in, backchannels in the service; `/api/voice/*`; harness voice mode.
4. **UI**: mic button, captions, Settings → Voice with characters.
5. **Real-voice corpus eval and tuning**; hands-free and wake word after that.

## 7. Decisions needed from you

1. Approve native audio in the Mac app (Swift) as the product path — the only way to remove self-interruption properly.
2. Approve Google News RSS (plus publisher feeds) as the dated news source, or name another provider.
3. Push-to-talk first (recommended) or always-listening from the start.
4. Record the real-voice corpus (Phase 0) — about an hour of your time, kept on this Mac only.
