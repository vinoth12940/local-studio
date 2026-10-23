# Voice assistant ("Jarvis") design

Status: proposal, nothing built. Everything is behind flags that default to off.

## Goal

Hands-free, real-time spoken conversation with the existing Local Studio agent harness. Anything the harness
does by typing (file search, document reads, coding, computer use, memory) works by voice. All audio and models
stay on the Mac.

Non-goals: cloud speech APIs, recording or storing audio, replacing the chat engine, a native speech-to-speech
model in the first release.

## Architecture

```
 Browser / WKWebView                 Local Studio (FastAPI, :7860)                 Voice service (:8084)
┌──────────────────────┐        ┌──────────────────────────────────┐        ┌──────────────────────────┐
│ mic (AEC, 16 kHz)    │  WS    │ api/voice.py   /api/voice/ws     │  WS    │ voice/service.py         │
│ AudioWorklet frames ─┼───────►│ services/voice.py (orchestrator) ├───────►│  wake word               │
│ player (PCM queue)  ◄┼────────┤  state machine, turn manager     │◄───────┤  VAD + turn end          │
│ orb / captions UI    │        │  sentence splitter, barge-in     │  HTTP  │  STT backend (pluggable) │
└──────────────────────┘        │  progress phrases, approvals     ├───────►│  TTS backend (pluggable) │
                                │            │                     │        └──────────────────────────┘
                                │            ▼                     │
                                │  chat turn runner (same code as  │
                                │  POST /api/chat): tools, gate,   │
                                │  memory, engine ensure           │
                                └──────────────────────────────────┘
```

The voice service is a separate process, like search (`search.py`, :8083): it holds the speech models, can crash
or be stopped without touching chat, and is exempt from the one-engine rule because its models are small.
It is launched only by `studio/services/voice.py` (`_start_voice`), always with relative file names and
`cwd=ROOT`. `stop.sh` and `_stop_voice` find it by the literal command line; keep those patterns in sync.
Dependencies live in `voice-requirements.txt` (exact pins), installed by `setup.sh --voice` when the user turns
voice on, never by `--install`. Development uses `LOCAL_STUDIO_VOICE_PORT` (for example 8094) so a dev server never
reaches the voice service of the running app.

## Components

### Client (`frontend/chat/voice.mjs`)
- `getUserMedia({audio: {echoCancellation, noiseSuppression, autoGainControl}})`. Echo cancellation is what makes
  barge-in work without headphones.
- An AudioWorklet resamples to 16 kHz mono PCM16 and posts 20 ms frames over one WebSocket.
- A playback queue plays PCM chunks from the server; `flush` drops the queue at once (barge-in).
- UI: an orb with idle / listening / thinking / speaking states, live captions of both sides, a mute button, an
  always-visible mic indicator. Voice turns are ordinary chat messages (`via: "voice"`), so history, search and
  Revert turn keep working.
- Mac app: `main.swift` must grant WKWebView media capture (`requestMediaCapturePermissionFor`), the bundle needs
  `NSMicrophoneUsageDescription`, and the hardened runtime needs the audio-input entitlement.

### Orchestrator (`studio/services/voice.py`, `studio/api/voice.py`)
One `VoiceSession` per WebSocket. It owns the state machine, calls the chat turn runner with the final transcript,
and turns the SSE stream into speech.

States: `IDLE` (waiting for wake word or push-to-talk) → `LISTENING` → `THINKING` → `SPEAKING` →
`FOLLOW_UP` (about 8 s of open mic without a wake word) → `IDLE`. Barge-in moves `SPEAKING`/`THINKING` →
`LISTENING`.

Mapping from chat events:

| Chat event | Voice behavior |
|---|---|
| text delta | buffer, split at sentence boundaries, send each sentence to TTS |
| `x_tool_*` / `x_plan` | short progress phrase if nothing was spoken for about 2 s ("Checking your files") |
| `x_approval` | default: speak "I need your approval on screen" and wait for the click; optional spoken yes/no |
| `x_question` | speak the question, listen for the answer, send it as the reply |
| `x_checkpoint`, `x_agent` | show in UI only; `use_computer` is announced in one line |
| stream end | `FOLLOW_UP` |

Barge-in: VAD fires while `SPEAKING`/`THINKING` → send `flush` to the client, cancel the TTS stream, cancel the chat
request (same path as Stop), go to `LISTENING`. A spoken "stop" or "cancel" alone does the same without starting a turn.

A voice system note (`VOICE_NOTE`, added in `_with_agent_note()` only for voice turns): short sentences, no markdown,
no code blocks or URLs, put long output on screen and say so.

### Voice service (`voice/`)
Pluggable backends behind two small interfaces; the orchestrator never imports a model library.

| Interface | Methods | Default | Alternatives |
|---|---|---|---|
| `WakeWord` | `feed(pcm) -> bool` | openWakeWord | push-to-talk only |
| `Vad` | `feed(pcm) -> speech_start / speech_end` | Silero VAD | energy VAD (first spike) |
| `Stt` | `stream(pcm) -> partial / final` | Parakeet or Phonon-2 (English) | Whisper turbo (multilingual) |
| `Tts` | `speak(text, voice) -> PCM chunks` | Kokoro-82M | Piper; Gradium Phonon if it becomes public |

Gradium Phonon is text-to-speech only, so it can never fill the `Stt` slot. Phonon-2 (Fermion Research) is a
light English speech-to-text model for Apple Silicon; its license and weight availability must be confirmed in the
spike before it is pinned. Whisper is the multilingual fallback, not the default.

Interface to the orchestrator: one WebSocket for the audio stream and events (`wake`, `speech_start`, `partial`,
`final`, `speech_end`), plus a streaming HTTP endpoint for TTS. `GET /health` for readiness.

## Wire protocol (browser ↔ `/api/voice/ws`)

Binary frames up: PCM16 16 kHz mono, 20 ms. JSON text frames both ways:

```
up:   {"t":"start","chat_id":"…","mode":"ptt|wake","voice":"af_heart"}
      {"t":"ptt","down":true|false}   {"t":"mute","on":true}   {"t":"approve","id":"…","ok":true}   {"t":"stop"}
down: {"t":"state","v":"listening|thinking|speaking|idle"}
      {"t":"caption","who":"user|bot","text":"…","final":false}
      {"t":"audio","seq":12,"sr":24000}  + binary PCM16 chunk
      {"t":"flush"}   {"t":"approval","id":"…","summary":"…"}   {"t":"error","msg":"…"}
```

## Latency budget (target about 1 s to first audio)

| Step | Budget |
|---|---|
| end-of-turn detection (silence + VAD) | 300–500 ms |
| STT finalization (streaming, so mostly done) | 100–150 ms |
| LLM first sentence | 200–400 ms (engine dependent) |
| TTS first chunk | 100 ms |

Stages overlap: TTS starts on the first sentence, and the engine is kept warm. Chat engine speed dominates; large
models will miss the target, and the UI should show "thinking" instead of silence.

## Security and privacy

- `/api/voice/ws` goes through the same `LocalOnly` rules: loopback `Host`, no foreign `Origin`. Confirm the guard
  covers WebSocket scopes (it must be checked, not assumed). The voice service refuses any request with an `Origin`.
- Audio is never written to disk and never logged. Only text transcripts reach chat storage. A debug option to dump
  audio is off and not exposed in the UI.
- Voice is a new untrusted input channel (a TV, another person, a passing sentence). Therefore it does not widen
  permissions: `harness_gate` is unchanged, `edit` and `exec` still ask, and the default is that approvals are
  clicked on screen. `voice.spoken_approvals` (off by default) allows a spoken "yes" for the `safe`/`risky` tiers
  only, after a confirmation phrase that repeats the action; `blocked` can never be approved by voice.
- Wake word runs in the voice service on the local stream only while the mic is enabled; the UI always shows the
  mic state, and Esc / the mute button cut it.
- Computer use by voice keeps every CuaDriver fence and app tier; the run card still appears.

## Flags (`studio/storage/flags.py`, `FLAGS`)

`voice.enabled` (false), `voice.wake_word` (false), `voice.spoken_approvals` (false), `voice.stt` / `voice.tts`
(backend names). New routes update `macos/fixtures/routes.json`, new events update `sse_contract.json`.

## Plan

| Phase | Deliverable | Exit criteria |
|---|---|---|
| 0 spike (no repo changes) | Confirm Phonon-2 / Parakeet / Whisper / Kokoro run under MLX on the target Mac; measure memory and latency; confirm pins and licenses | numbers written into this doc |
| 1 push-to-talk | voice service with Silero + STT + Kokoro; `/api/voice/ws`; mic button in the composer; sentence-chunked speech; barge-in; `VOICE_NOTE`; Mac app mic permission | spoken question gets a spoken answer on the 7861 dev server; Stop and barge-in cancel within about 300 ms |
| 2 hands-free | wake word, follow-up window, progress phrases from tool events, orb UI, captions | tool turn speaks progress; wake word false-accept rate acceptable in a quiet-room test |
| 3 approvals and polish | on-screen approval prompt during voice turns, optional spoken approvals, `x_question` by voice, voice and language settings, Mac mini window | approvals cannot be satisfied by anything blocked; flag-off build is unchanged |
| 4 optional | Moshi-style native speech-to-speech backend, multilingual TTS | decided after phase 3 usage |

## Tests

New `macos/check_voice.py` (wired into `build.sh`), offline with fake `Stt`/`Tts`/`Vad`: state machine transitions,
sentence splitter (abbreviations, numbers, code fences skipped), barge-in cancels the chat request and flushes audio,
progress phrase timing, approval path never auto-approves, `LocalOnly` rejects bad `Host`/`Origin` on the WebSocket,
audio never reaches disk. `check_ui.js` covers the module graph; mic and playback are verified by hand in the dev
server on port 7861 with its own `LOCAL_STUDIO_HOME` and never against the running app. `build.sh` copy list gets
`voice/` and `voice-requirements.txt`; `stop.sh` gets the new process pattern.

## Risks

- Echo and self-interruption on speakers: rely on browser AEC, fall back to push-to-talk or headphones.
- Memory: STT + TTS + chat engine on a 16 GB Mac; check `sysRam` before starting voice and offer a smaller STT.
- Chat engine latency dominates the experience; small models answer faster by voice.
- WKWebView media permissions and entitlements differ from a browser; test in the built app, not only the dev server.
- Wake-word false accepts act on stray speech: keep the gate and approvals as the real safety layer.

## Open decisions

1. Default STT: Phonon-2 (light, English only) vs Parakeet vs Whisper turbo (multilingual).
2. Spoken approvals: on-screen only (recommended) or allow spoken yes/no for safe/risky.
3. Wake phrase and whether wake word ships in phase 2 or push-to-talk is enough at first.
4. Languages needed at launch (decides Whisper vs English-only models).
