"""The live-facts check for spoken turns: ask d1-2B (decide.py), fall back to word rules, pick the search to start at once.

Before a voice turn's first model round, d1 answers five yes/no questions about what was said in one pass (~0.1 s on an
M4 Pro). When the turn clearly needs live public facts, the chat route runs the search itself and hands the result to the
model as its first tool result: one model round fewer, and no "I don't have live data". A question about the user's own
documents only ever searches their files, never the web. Any d1 error or timeout uses the rules for that turn.
"""

import os
import re
import secrets
import signal
import subprocess
import time

import httpx

from studio import config
from studio.config import DSPARK_PY, HF_CLI, LOGS, ROOT
from studio.infra.logs import open_log
from studio.infra.system import _pid_alive, _read_pid, system_memory
from studio.services.models import snapshot_file
from studio.storage.flags import flag

QUESTIONS = {
    "live_facts": {"type": "noul", "instructions": "Does answering need live or changing public information, such as news, prices, "
                                                   "weather, scores, releases, or anything latest, today or now?"},
    "my_files": {"type": "noul", "instructions": "Is the user asking about their own documents or files on this computer?"},
    "small_talk": {"type": "noul", "instructions": "Is this small talk or a greeting that needs no facts?"},
    "standalone": {"type": "noul", "instructions": "Can the last message be understood on its own, without the earlier conversation?"},
    "news": {"type": "noul", "instructions": "Is the user asking for news headlines?"},
}                                                     # "noul": d1's yes/no question, answered with the probability of yes
# Measured on 28 Oct with d1-2B on the 51 labeled questions in macos/fixtures/route_questions.json: live >= 0.3 caught 29 of 30
# with 0 of 21 false alarms (a needless search costs a second; a missed one costs a refusal); everyday questions score at most
# 0.18 for my_files, personal ones 0.42-0.69; follow-ups score 0.35-0.36 for standalone, "my passport" 0.49.
LIVE_THRESHOLD, FILES_THRESHOLD, STANDALONE_THRESHOLD = 0.3, 0.35, 0.45

_LIVE = re.compile(r"\b(latest|today|tonight|now|current(?:ly)?|prices?|cost|cheaper|cheapest|selling|how much|weather|forecast|"
                   r"typhoon|storm|hurricane|earthquake|scores?|news|headlines?|release[sd]?|updates?|new|next|happened|won|"
                   r"trending|open|close[sd]?|closing|near me|crowded|playing|are out|leads?|doing|weekends?|last night|"
                   r"this (?:week|month|year)|yesterday|tomorrow|stocks?|exchange rate)\b|\bwho (?:is|are|runs|leads|owns|won)\b", re.I)
_FILES = re.compile(r"\bmy\b.*\b(passport|visa|i-?94|ead|tax(?:es)?|w-?2|1099|documents?|files?|pdf|statements?|receipts?|"
                    r"contracts?|lease)\b", re.I)
_FOLLOW_UP = re.compile(r"^(and|what about|how about|also|then)\b", re.I)
_NEWS = re.compile(r"\b(news|headlines?)\b", re.I)
_GREETING = re.compile(r"^(hi|hello|hey|thanks|thank you|good (?:morning|afternoon|evening|night))\b", re.I)
_FILLERS = re.compile(r",?\s*\b(?:um+|uh+|erm+|hmm+|you know)\b,?|,?\s*\blike,", re.I)


def _decide_token():
    """The secret decide.py requires, created once with owner-only permissions."""
    try:
        token = config.DECIDE_TOKEN.read_text().strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    config.DECIDE_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(config.DECIDE_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def clean_query(text):
    """What was said, without spoken fillers ("um, what's the, uh, price" -> "what's the price")."""
    return " ".join(_FILLERS.sub(" ", str(text or "")).split()).strip(" ,")


def rules(text):
    """The same five answers from word rules, each 0.0 or 1.0: the fallback whenever d1 is off, down or slow."""
    said = clean_query(text)
    return {"live_facts": float(bool(_LIVE.search(said))), "my_files": float(bool(_FILES.search(said))),
            "small_talk": float(bool(_GREETING.match(said))),
            "standalone": 0.0 if len(said.split()) < 3 or _FOLLOW_UP.match(said) else 1.0,
            "news": float(bool(_NEWS.search(said))), "source": "rules", "ms": 0}


def preflight(text, previous=None, timeout=0.25):
    """d1's answers for this turn (with the previous user turn as context), or the rules' when d1 does not answer in time."""
    started = time.perf_counter()
    state = f"Earlier: {previous[:300]}\nLast message: {text[:500]}" if previous else str(text)[:500]
    try:
        r = httpx.post(config.DECIDE_URL + "/decide", json={"state": state, "questions": QUESTIONS},
                       headers={"Authorization": "Bearer " + _decide_token()}, timeout=timeout)
        r.raise_for_status()
        answers = r.json()["answers"]
        plan = {name: float(answers[name]["noul"]) for name in QUESTIONS}
        return {**plan, "source": "d1", "ms": int((time.perf_counter() - started) * 1000)}
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return {**rules(text), "ms": int((time.perf_counter() - started) * 1000)}


def prefetch_call(plan, text, web_fast, files=False):
    """The read tool to run before round one, as (name, args), or None to let the model choose as usual."""
    query = clean_query(text)
    if not query or plan["standalone"] < STANDALONE_THRESHOLD:
        return None                                   # a follow-up's words alone make a junk query
    if plan["my_files"] >= FILES_THRESHOLD:
        return ("search_files", {"query": query}) if files else None      # personal questions never go to the web
    if plan["live_facts"] < LIVE_THRESHOLD:
        return None
    if web_fast and plan["news"] >= 0.5:
        return "news_search", {"query": query, "since": "1d"}
    return "web_search", {"query": query}


# ---------------------------------------------------------------- the decide.py process

DECIDE_PATTERN = rf"^[^ ]*python[^ ]* decide.py --port {config.DECIDE_PORT}"   # stop.sh has the same pattern: keep in sync
MIN_FREE_PERCENT = 20                     # macOS's free percentage (it counts reclaimable pages); 40 % on 28 Oct with Qwen at 256K
_last = {"reason": ""}                    # why the last start did not run d1, for the Voice view


def decide_command():
    # Relative file name with cwd=ROOT: the bundled path contains a space, which would defeat stop.sh's pkill pattern.
    return [str(DSPARK_PY), "decide.py", "--port", str(config.DECIDE_PORT)]


def _model_present():
    return snapshot_file(config.DECIDE_MODEL, "config.json") is not None


def _fetch_model():
    """Fetch d1-2B (1.35 GB) into the shared Hugging Face cache, once, when the owner turns the feature on."""
    with open_log(LOGS / "decide-setup.log", "w") as log:
        return subprocess.run([str(HF_CLI), "download", config.DECIDE_MODEL], stdout=log, stderr=subprocess.STDOUT).returncode == 0


def _spawn():
    log = open_log(LOGS / "decide.log", "a")
    proc = subprocess.Popen(decide_command(), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                            env={**os.environ, "LOCAL_STUDIO_DECIDE_TOKEN": _decide_token(), "LOCAL_STUDIO_DECIDE_MODEL": config.DECIDE_MODEL,
                                 "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    (LOGS / "decide.pid").write_text(str(proc.pid))


def _process():
    return subprocess.run(["pgrep", "-f", DECIDE_PATTERN], capture_output=True).returncode == 0


def _health():
    try:
        r = httpx.get(config.DECIDE_URL + "/health", timeout=1)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def start_decide():
    """Start d1 beside Voice when the flag is on and memory allows; returns mode(). The 256K window is never lowered for it."""
    if not flag("voice.decide"):
        return mode()
    try:
        free = system_memory().get("free_percent")
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        free = None                           # an unreadable report never blocks d1
    if free is not None and free < MIN_FREE_PERCENT:
        _last["reason"] = f"memory: {free}% free"
        return {"mode": "rules", "reason": _last["reason"]}
    if not _model_present() and not _fetch_model():
        _last["reason"] = "d1 could not be downloaded (see logs/decide-setup.log)"
        return {"mode": "rules", "reason": _last["reason"]}
    _last["reason"] = ""
    if not _process():
        _spawn()
    return mode()


def stop_decide():
    pid = _read_pid("decide")
    if pid and _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
    subprocess.run(["pkill", "-f", DECIDE_PATTERN])
    (LOGS / "decide.pid").unlink(missing_ok=True)


def mode():
    """Which live-facts check spoken turns use now: {"mode": "d1" | "rules" | "off", "reason"}."""
    if not flag("voice.decide"):
        return {"mode": "off", "reason": ""}
    health = _health()
    if health and health.get("loaded"):
        return {"mode": "d1", "reason": ""}
    reason = _last["reason"] or (health.get("error") or "d1 is loading" if health else "d1 is not running")
    return {"mode": "rules", "reason": reason}
