"""Attach-only timing and grounding of spoken tool turns: every part of a turn, for the model already loaded.

Runs the real /api/chat in voice mode (the body a spoken turn sends) against the engine Local Studio already has loaded, or
against --engine-url (a test engine you started yourself under a command line the app's engine patterns don't match). It
never starts or stops an engine: LOCAL_STUDIO_EVAL_ATTACH=1, and _start_engine/_stop_engine raise. Everything else is
isolated: a throwaway LOCAL_STUDIO_HOME, its own app port, search and voice ports that point nowhere. Speech is not in these
numbers (Parakeet ~0.07 s, Phonon first audio ~0.1 s: macos/check_voice_e2e.py measures those).

Every read tool's real output is logged, so each number in an answer, in digits or spoken ("eighty-one thousand"), is
checked against what the tools returned. A JSON report goes to build/voice-turns/.

    TMPDIR=/tmp uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_voice_turns.py \
        <model_id> [--flag harness.web_fast ...] [--engine-mode vision|gguf --engine-url URL] [--repeat N]

Exit 0 after measuring (this is a measurement, not a pass/fail gate); 2 when no engine answers.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ap = argparse.ArgumentParser()
ap.add_argument("model_id")
ap.add_argument("--out")
ap.add_argument("--flag", action="append", default=[])
ap.add_argument("--engine-mode")
ap.add_argument("--engine-url")
ap.add_argument("--repeat", type=int, default=2)
ap.add_argument("--port", type=int, default=7893)
args = ap.parse_args()

_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                     "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_SCALES = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}


def spoken_numbers(text):
    """Numbers written as words, in order: "eighty-one thousand" -> 81000, "two point five" -> 2.5. Digits are not read here."""
    found, words = [], re.findall(r"[a-z]+", text.lower().replace("-", " "))
    total = current = None
    decimals, after_point = "", False

    def close():
        nonlocal total, current, decimals, after_point
        if current is not None or total is not None:
            value = (total or 0) + (current or 0)
            found.append(float(f"{value}.{decimals}") if decimals else value)
        total = current = None
        decimals, after_point = "", False

    for i, word in enumerate(words):
        if after_point and word in _UNITS and _UNITS[word] < 10:
            decimals += str(_UNITS[word])
        elif word in _UNITS or word in _TENS:
            if after_point:
                close()
            current = (current or 0) + _UNITS.get(word, _TENS.get(word, 0))
        elif word == "hundred" and current is not None:
            current *= 100
        elif word in _SCALES and (current is not None or total is not None):
            total, current = (total or 0) + (current or 1) * _SCALES[word], None
        elif word == "point" and (current is not None or total is not None) and i + 1 < len(words) and words[i + 1] in _UNITS:
            after_point = True
        elif word == "and" and current is not None and i + 1 < len(words) and (words[i + 1] in _UNITS or words[i + 1] in _TENS):
            continue
        else:
            close()
    close()
    return found


def grounded_spoken(numbers, tool_text):
    """True when every spoken number (10 or more) is within 2 % of a number in the tools' output."""
    source = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", tool_text.replace(",", ""))]
    return all(any(abs(s - n) <= 0.02 * n for s in source) for n in numbers if n >= 10)


_NUMBER_WORDS = r"\b(?:" + "|".join(list(_UNITS)[10:] + list(_TENS) + ["hundred", "thousand", "million"]) + r")\b"
EXPECT = {"capital": r"\bparis\b", "times": r"\b(56|fifty[- ]six)\b", "intro": r".", "name": r"\bvinoth\b",
          "clock": r"\d|o'clock|noon|midnight|half past|quarter", "price": r"\d|" + _NUMBER_WORDS, "weather": r"\d|" + _NUMBER_WORDS,
          "llama": r"\bv?\d+\.\d+\.\d+\b|\bb\d{4,}\b", "news": r"."}
REFUSAL = re.compile(r"\b(?:don't|do not|can't|cannot|unable to)\b.{0,40}\b(?:access|live|real[- ]time|current|browse)\b", re.I)
NUM = re.compile(r"\b(?:b\d{4,}|\d+(?:\.\d+)+|\d[\d,]*\d|\d)\b")


def grounding(reply, tool_text):
    """Numbers in the answer, in digits or spoken, that the tools' output does not contain. Small counts (<10) are skipped."""
    src = tool_text.replace(",", "")
    nums = [n.replace(",", "") for n in NUM.findall(reply)]
    nums = [n for n in nums if not (n.isdigit() and int(n) < 10)]
    spoken = [n for n in spoken_numbers(reply) if n >= 10]
    return {"numbers": nums, "spoken": spoken, "not_in_sources": [n for n in nums if n not in src] +
            [f"{n:g} (spoken)" for n in spoken if not grounded_spoken([n], tool_text)]}


# (conversation, what is said, which EXPECT pattern the answer must match, needs fresh facts)
CASES = [
    ("a", "What is the capital of France?", "capital", False),
    ("c", "What is seven times eight?", "times", False),
    ("b", "My name is Vinoth and I live in Chennai.", "intro", False),
    ("b", "What is my name?", "name", False),
    ("t", "What time is it right now?", "clock", False),
    ("p", "What is the price of bitcoin right now?", "price", True),
    ("w", "What is the weather in Chennai right now?", "weather", True),
    ("d", "What is the latest release of llama dot cpp?", "llama", True),
    ("n", "What is the latest cricket news?", "news", True),
]

# Self-test of the scoring, before anything starts.
assert spoken_numbers("about eighty-one thousand dollars, up two point five percent") == [81000, 2.5]
assert spoken_numbers("twenty nine degrees, feels like thirty seven") == [29, 37]
assert grounded_spoken([81000], "price $81,066.58") and not grounded_spoken([95000], "price $81,066.58")
assert re.search(EXPECT["llama"], "the latest is v0.8.0, build b11969")

TMP = Path(tempfile.mkdtemp(prefix="ls-measure-", dir="/tmp")).resolve()
STATE = TMP / "state"
(STATE / "data").mkdir(parents=True)
(STATE / "logs").mkdir(parents=True)
os.environ.update(LOCAL_STUDIO_HOME=str(STATE), LOCAL_STUDIO_EVAL_ATTACH="1", LOCAL_STUDIO_SEARCH_PORT="8099",
                  LOCAL_STUDIO_VOICE_PORT="8092", LOCAL_STUDIO_DECIDE_PORT="8091")
SETTINGS = {"voice": {"enabled": True}, "chat_model": args.model_id}
for name in args.flag:                                    # "harness.web_fast" -> {"harness": {"web_fast": true}}
    section, key = name.split(".", 1)
    SETTINGS.setdefault(section, {})[key] = True
(STATE / "data" / "settings.json").write_text(json.dumps(SETTINGS))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from studio import config  # noqa: E402
from studio.services import engines, harness, models  # noqa: E402


def _no_engine(*a, **k):
    raise RuntimeError("the measurement must never start or stop an engine (attach-only)")


engines._start_engine = engines._stop_engine = _no_engine
models.model_installed = lambda m: True               # the model lives in a scratch cache or in the running app
if args.engine_url:
    config.ENGINES[args.engine_mode]["url"] = args.engine_url
TOOL_LOG = []
_real_read = harness._chat_read_tool


def _logged(name, targs, timezone=None):
    t = time.monotonic()
    out = _real_read(name, targs, timezone)
    TOOL_LOG.append({"name": name, "args": targs, "s": round(time.monotonic() - t, 2), "out": json.dumps(out)[:30000]})
    return out


harness._chat_read_tool = _logged
_CHARS = re.search(r"CHARACTERS = (\[.*?\]);", (ROOT / "frontend/voice/characters.mjs").read_text(), re.S).group(1)
STYLE = next(c["style"] for c in json.loads(re.sub(r",\s*\]$", "]", _CHARS)) if c["id"] == "jarvis")

def truths():
    """What is true right now, fetched independently of the model's tools."""
    out = {}
    try:
        out["llama.cpp"] = httpx.get("https://api.github.com/repos/ggml-org/llama.cpp/releases/latest", timeout=15).json().get("tag_name")
    except Exception as e:
        out["llama.cpp"] = f"unavailable: {e}"
    try:
        out["bitcoin_usd"] = httpx.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=15).json()["bitcoin"]["usd"]
    except Exception as e:
        out["bitcoin_usd"] = f"unavailable: {e}"
    return out


def chat(client, messages, chat_id):
    body = {"model_id": args.model_id, "chat_id": chat_id, "messages": messages, "max_tokens": 2048, "enable_thinking": False,
            "voice": True, "voice_style": STYLE, "timezone": "Asia/Kolkata"}
    t0 = time.monotonic()
    marks, text, round_text, first, first_sentence, errors, stats = [], "", "", None, None, [], None
    in_round = False
    with client.stream("POST", f"http://127.0.0.1:{args.port}/api/chat", json=body, timeout=300) as r:
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.read().decode(errors='replace')[:300]}"}
        for line in r.iter_lines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            try:
                ev = json.loads(line[5:])
            except ValueError:
                continue
            now = round(time.monotonic() - t0, 3)
            if ev.get("error"):
                errors.append(str(ev["error"])[:300])
            if ev.get("x_engine"):
                marks.append((now, "engine", ev["x_engine"].get("status")))
            if ev.get("x_mlx_dspark"):
                stats = ev["x_mlx_dspark"]
            if ev.get("x_tool"):
                xt = ev["x_tool"]
                marks.append((now, "tool_" + xt.get("status", "?"), xt.get("name")))
                if xt.get("status") == "running":
                    round_text = ""
            ch = (ev.get("choices") or [{}])[0]
            d = (ch.get("delta") or {}).get("content")
            if d:
                if not in_round:
                    marks.append((now, "first_text", None))
                    in_round = True
                first = first if first is not None else now
                round_text += d
                text += d
                if first_sentence is None and re.search(r"[.!?](\s|$)", round_text):
                    first_sentence = now
            if ch.get("finish_reason"):
                marks.append((now, "finish", ch["finish_reason"]))
                in_round = False
                if ch["finish_reason"] == "tool_calls":
                    round_text = ""
    total = round(time.monotonic() - t0, 3)
    # Split the turn into model time and tool time: a tool runs from its "running" mark to its "done"/"error" mark.
    tool_s, open_at = 0.0, {}
    for t, kind, name in marks:
        if kind == "tool_running":
            open_at.setdefault(name, []).append(t)
        elif kind in ("tool_done", "tool_error") and open_at.get(name):
            tool_s += t - open_at[name].pop(0)
    return {"reply": (round_text.strip() or text.strip()), "first_token_s": first, "first_sentence_s": first_sentence,
            "total_s": total, "tool_s": round(tool_s, 2), "model_s": round(total - tool_s, 2),
            "tools": [n for _, k, n in marks if k == "tool_running"], "marks": marks, "errors": errors, "stats": stats}


def serve():
    from studio.main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        try:
            if httpx.get(f"http://127.0.0.1:{args.port}/api/health", timeout=1).status_code < 500:
                return server
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("the measurement app server did not start")


def summary(runs):
    """What the goals are measured on: web-turn time, refusals, ungrounded numbers, the slowest no-tool turn."""
    web = sorted(r["total_s"] for r in runs if r.get("fresh") and r.get("used_web") and r.get("total_s") is not None)
    plain = [r["total_s"] for r in runs if not r.get("fresh") and not r.get("tools") and r.get("total_s") is not None]
    return {"web_turns": len(web), "median_web_s": web[len(web) // 2] if web else None,
            "refused": sum(1 for r in runs if r.get("fresh") and not r.get("used_web") and REFUSAL.search(r.get("reply", ""))),
            "skipped_search": sum(1 for r in runs if r.get("fresh") and not r.get("used_web")),
            "ungrounded": sum(len(r.get("grounding", {}).get("not_in_sources", [])) for r in runs),
            "slowest_no_tool_s": max(plain) if plain else None, "wrong": [r["said"] for r in runs if not r.get("ok")]}


def main():
    m = models.chat_model(args.model_id)
    mode = args.engine_mode or models.engine_mode(m)
    if not engines._engine_ready(mode):
        print(f"   blocked: no {mode} engine answers at {config.ENGINES[mode]['url']} (load {m['name']} in Local Studio, or pass --engine-url)")
        return 2
    out = Path(args.out) if args.out else ROOT / "build" / "voice-turns" / f"{args.model_id}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    server = serve()
    truth = truths()
    runs = []
    with httpx.Client() as client:
        # warm the engine's prompt cache the way the Voice view does (one token, same instructions and tools)
        client.post(f"http://127.0.0.1:{args.port}/api/chat", json={"model_id": args.model_id, "chat_id": uuid.uuid4().hex[:16],
                    "messages": [{"role": "user", "content": "Hello."}], "warm": True, "voice": True, "voice_style": STYLE,
                    "enable_thinking": False, "timezone": "Asia/Kolkata"}, timeout=300)
        for rep in range(args.repeat):
            convs = {}
            for conv, said, expect, fresh in CASES:
                hist = convs.setdefault(conv, {"id": uuid.uuid4().hex[:16], "messages": []})
                hist["messages"].append({"role": "user", "content": said})
                before = len(TOOL_LOG)
                res = chat(client, hist["messages"], hist["id"])
                used = TOOL_LOG[before:]
                res.update(rep=rep, said=said, fresh=fresh, ok=bool(re.search(EXPECT[expect], res.get("reply", ""), re.I)),
                           tool_calls=[{"name": u["name"], "args": u["args"], "s": u["s"]} for u in used])
                if fresh:
                    res["grounding"] = grounding(res.get("reply", ""), " ".join(u["out"] for u in used))
                    res["used_web"] = any(u["name"] in ("web_search", "read_webpage", "news_search") for u in used)
                hist["messages"].append({"role": "assistant", "content": res.get("reply", "")})
                runs.append(res)
                print(f"   {rep} {said[:44]:44} total {res.get('total_s')!s:>6}s model {res.get('model_s')!s:>6}s tools {res.get('tool_s')!s:>5}s"
                      f" {','.join(res.get('tools', [])) or '-':28} ok={res.get('ok')} {res.get('grounding', {}).get('not_in_sources', '')}", flush=True)
    result = summary(runs)
    out.write_text(json.dumps({"model": args.model_id, "engine_mode": mode, "flags": args.flag, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "truth": truth, "summary": result, "runs": runs, "tool_log": TOOL_LOG}, indent=1))
    server.should_exit = True
    print(f"   summary {json.dumps(result)}")
    print("   wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
