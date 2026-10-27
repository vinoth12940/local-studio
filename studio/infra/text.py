"""Small text helpers shared by the agent views and the gate.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""


def est_tokens(s):
    return int(len(s) / 3.2)                                 # the same heuristic /api/chat uses


def _clip(s, n):
    s = " ".join(str(s or "").split())                       # one line, no stray control whitespace
    return s if len(s) <= n else s[:n - 1] + "…"
