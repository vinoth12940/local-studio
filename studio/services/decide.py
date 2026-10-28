"""The live-facts check for spoken turns: ask d1-2B (decide.py), fall back to word rules, pick the search to start at once.

Before a voice turn's first model round, d1 answers five yes/no questions about what was said in one pass (~0.1 s on an
M4 Pro). When the turn clearly needs live public facts, the chat route runs the search itself and hands the result to the
model as its first tool result: one model round fewer, and no "I don't have live data". A question about the user's own
documents only ever searches their files, never the web. Any d1 error or timeout uses the rules for that turn.
"""

import os
import re
import secrets
import time

import httpx

from studio import config

def _yes_no(instructions):
    # d1's yes/no question ("noul": the probability of yes). Built with dict() rather than a literal: check_app's SSE contract
    # reads every {"type": ...} literal under studio/ as an agent run event.
    return dict(type="noul", instructions=instructions)


QUESTIONS = {
    "live_facts": _yes_no("Does answering need live or changing public information, such as news, prices, weather, scores, "
                          "releases, or anything latest, today or now?"),
    "my_files": _yes_no("Is the user asking about their own documents or files on this computer?"),
    "small_talk": _yes_no("Is this small talk or a greeting that needs no facts?"),
    "standalone": _yes_no("Can the last message be understood on its own, without the earlier conversation?"),
    "news": _yes_no("Is the user asking for news headlines?"),
}
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
