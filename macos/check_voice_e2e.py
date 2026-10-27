"""End-to-end voice check with the real parts: speech -> recognition -> the chat route -> your loaded model -> speech.

Each question is spoken by Phonon (the app's own voice), recognized by Parakeet through a real voice service on a test
port, sent through the real /api/chat (a real server on a test port, with the request a spoken turn makes) to the model
already loaded in Local Studio, and the reply is spoken again. It measures what a person would notice: was the question
heard right, did an answer come, was it short plain speech, did it use the web when it had to, did it remember, and how
long until the first sound.

The model is used attach-only (LOCAL_STUDIO_EVAL_ATTACH=1, and starting or stopping any engine raises): load a chat model
in Local Studio first. Everything else is isolated: a throwaway LOCAL_STUDIO_HOME, a voice service on port 8095 and an
app server on port 7881, both stopped at the end. The microphone is never opened.

    TMPDIR=/tmp uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_voice_e2e.py

Exit 0: every case passed; 1: a case failed; 2: blocked (no model loaded). A JSON report goes to build/voice-e2e/.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="ls-e2e-", dir="/tmp")).resolve()
STATE = TMP / "state"
VOICE_PORT, APP_PORT = 8095, 7881
LIVE = Path.home() / "Library" / "Application Support" / "Local Studio"

os.environ.update(LOCAL_STUDIO_HOME=str(STATE), LOCAL_STUDIO_VOICE_PORT=str(VOICE_PORT), LOCAL_STUDIO_EVAL_ATTACH="1",
                  LOCAL_STUDIO_SEARCH_PORT="8096")
(STATE / "data").mkdir(parents=True)
(STATE / "logs").mkdir(parents=True)


def live_model_id():
    try:
        return json.loads((LIVE / "logs" / "engine.json").read_text()).get("model_id")
    except (OSError, ValueError):
        return None


MODEL_ID = live_model_id()
(STATE / "data" / "settings.json").write_text(json.dumps({"voice": {"enabled": True}, "chat_model": MODEL_ID}))
(STATE / "data" / "voice-deps.ok").write_text(hashlib.sha256((ROOT / "voice-requirements.txt").read_bytes()).hexdigest())

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from studio.services import engines  # noqa: E402
from studio.services import voice as services_voice  # noqa: E402


def _no_engine(*args, **kwargs):
    raise RuntimeError("the end-to-end check must never start or stop an engine (attach-only)")


engines._start_engine = engines._stop_engine = _no_engine
_CHARS = re.search(r"CHARACTERS = (\[.*?\]);", (ROOT / "frontend/voice/characters.mjs").read_text(), re.S).group(1)
STYLE = next(c["style"] for c in json.loads(re.sub(r",\s*\]$", "]", _CHARS)) if c["id"] == "jarvis")   # the Voice view's default character
VOICE = "Edwin"

# (conversation, what is said, words the recognizer must hear, what the answer must contain, must it use the web)
CASES = [
    ("a", "What is the capital of France?", ["capital", "france"], r"\bparis\b", False),
    ("b", "My name is Vinoth and I live in Chennai.", ["vinoth", "chennai"], r".", False),
    ("b", "What is my name?", ["name"], r"\bvinoth\b", False),
    ("c", "What is seven times eight?", [("seven", "7"), ("eight", "8")], r"\b(56|fifty[- ]six)\b", False),
    ("d", "What is the latest release of llama dot cpp?", [("llama",), ("release", "version")], r"\bb\d{4,}\b|release|version", True),
]
MARKDOWN = re.compile(r"(^|\s)#{1,6}\s|\*\*|`|\|.*\||^\s*[-*•]\s|^\s*\d+\.\s", re.M)


def norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", s.lower())


def heard_ok(transcript, words):
    t = norm(transcript).split()
    return all(any(alt in t for alt in (w if isinstance(w, tuple) else (w,))) for w in words)


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def say(client, text):
    t = time.monotonic()
    r = client.post(f"http://127.0.0.1:{APP_PORT}/api/voice/say", json={"text": text, "voice": VOICE}, timeout=60)
    r.raise_for_status()
    return r.content, time.monotonic() - t


def transcribe(client, wav):
    t = time.monotonic()
    r = client.post(f"http://127.0.0.1:{APP_PORT}/api/voice/transcribe", content=wav, headers={"content-type": "audio/wav"}, timeout=60)
    r.raise_for_status()
    return r.json().get("text", ""), time.monotonic() - t


def chat(client, messages, chat_id):
    """One spoken turn through the real route, as the Voice view sends it; returns the reply and its timings."""
    body = {"model_id": MODEL_ID, "chat_id": chat_id, "messages": messages, "max_tokens": 2048, "enable_thinking": False,
            "voice": True, "voice_style": STYLE, "timezone": "Asia/Kolkata"}
    t0, first, first_sentence, text, tools, errors = time.monotonic(), None, None, "", [], []
    round_text = ""
    with client.stream("POST", f"http://127.0.0.1:{APP_PORT}/api/chat", json=body, timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            try:
                ev = json.loads(line[5:])
            except ValueError:
                continue
            if ev.get("error"):
                errors.append(str(ev["error"])[:300])
            if ev.get("x_tool") and ev["x_tool"].get("status") == "running":
                tools.append(ev["x_tool"].get("name")); round_text = ""
            ch = (ev.get("choices") or [{}])[0]
            d = (ch.get("delta") or {}).get("content")
            if d:
                first = first or time.monotonic() - t0
                round_text += d; text += d
                if first_sentence is None and re.search(r"[.!?](\s|$)", round_text):
                    first_sentence = time.monotonic() - t0
            if ch.get("finish_reason") == "tool_calls":
                round_text = ""
    final = round_text.strip() or text.strip()
    return {"reply": final, "first_token_s": first, "first_sentence_s": first_sentence or (time.monotonic() - t0),
            "total_s": time.monotonic() - t0, "tools": tools, "errors": errors}


def serve_app():
    from studio.main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=APP_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{APP_PORT}/api/voice/status", timeout=1).status_code == 200:
                return server
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("the test app server did not start")


def main():
    if not MODEL_ID or not engines._engine_ready("text"):
        print("   blocked: no chat model is loaded in Local Studio (load one, then run this again)")
        return 2
    print(f"   model: {MODEL_ID} (attach-only)")
    server = serve_app()
    client = httpx.Client(trust_env=False)
    report = {"model": MODEL_ID, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "cases": []}
    failures = []
    try:
        services_voice._start_voice()
        for _ in range(240):
            st = client.get(f"http://127.0.0.1:{APP_PORT}/api/voice/status").json()
            if st.get("running") and st.get("stt_loaded") and st.get("tts_loaded"):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("the test voice service did not become ready (see " + str(STATE / "logs/voice.log") + ")")
        convos = {}
        for conv, spoken, words, expect, needs_web in CASES:
            wav, synth_s = say(client, spoken)
            heard, stt_s = transcribe(client, wav)
            msgs = convos.setdefault(conv, [])
            msgs.append({"role": "user", "content": heard})
            out = chat(client, list(msgs), f"e2e{conv}{len(msgs)}")
            msgs.append({"role": "assistant", "content": out["reply"]})
            first = sentences(out["reply"])[0] if out["reply"] else ""
            tts_first_s = say(client, first)[1] if first else None
            case = {"said": spoken, "heard": heard, "stt_s": round(stt_s, 2), **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in out.items()},
                    "tts_first_s": round(tts_first_s, 2) if tts_first_s else None,
                    "first_audio_s": round(stt_s + out["first_sentence_s"] + (tts_first_s or 0), 2)}
            problems = []
            if not heard_ok(heard, words):
                problems.append(f"misheard: {heard!r}")
            if out["errors"]:
                problems.append("error: " + out["errors"][0])
            if not out["reply"]:
                problems.append("no reply")
            elif not re.search(expect, out["reply"], re.I):
                problems.append(f"answer lacks {expect}")
            if out["reply"] and MARKDOWN.search(out["reply"]):
                problems.append("markdown in a spoken reply")
            if len(sentences(out["reply"])) > 5:
                problems.append(f"{len(sentences(out['reply']))} sentences (too long to speak)")
            if needs_web and not any(t in ("web_search", "read_webpage") for t in out["tools"]):
                problems.append("answered a current-facts question without looking it up")
            case["problems"] = problems
            report["cases"].append(case)
            mark = "ok  " if not problems else "FAIL"
            print(f"   {mark} \"{spoken}\" heard \"{heard}\" ({stt_s:.2f}s) -> \"{out['reply'][:110]}\"")
            print(f"        first word {case['first_token_s']}s, first sentence {case['first_sentence_s']}s, first audio ~{case['first_audio_s']}s,"
                  f" total {case['total_s']}s, tools {out['tools'] or '-'}")
            for p in problems:
                print(f"        - {p}")
                failures.append((spoken, p))
    finally:
        services_voice._stop_voice()                    # the test port's service only (its patterns name port 8095)
        server.should_exit = True
        out_dir = ROOT / "build" / "voice-e2e"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(report, indent=2))
        print(f"   report: {path}")
    if failures:
        print(f"   voice end-to-end: {len(failures)} problem(s)")
        return 1
    print("   voice end-to-end OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
