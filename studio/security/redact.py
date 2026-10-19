"""Secret redaction for what tools print (flag security.redact, off by default; refactor P3).

When on, the chat's live tool output (x_tool_output) and the tool details shown in the step (x_tool) have API keys, tokens and
private keys replaced by [redacted ...] before they reach the page. What the tool itself did is unchanged.
"""
import re

_PATTERNS = [
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}")),
    ("api key", re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{20,}")),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)")),
]
_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END (?:[A-Z]+ )*PRIVATE KEY-----")


def redact(text):
    """The text with every secret-shaped token replaced; anything that is not a str comes back unchanged."""
    if not isinstance(text, str):
        return text
    for kind, rx in _PATTERNS:
        text = rx.sub(f"[redacted {kind}]", text)
    return text


def redact_obj(obj):
    """redact() applied to every string inside dicts and lists."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


class StreamRedactor:
    """Buffer incomplete lines so split tokens/PEM delimiters never escape the redactor."""

    def __init__(self):
        self.in_key = False
        self.pending = ""
        self.dropping = False

    def _line(self, line):
        if self.in_key:
            end = _PEM_END.search(line)
            if not end:
                return ""
            self.in_key = False
            return self._line(line[end.end():])
        begin = _PEM_BEGIN.search(line)
        if begin:
            end = _PEM_END.search(line, begin.end())
            if end:
                return redact(line[:begin.start()]) + "[redacted private key]" + self._line(line[end.end():])
            self.in_key = True
            return redact(line[:begin.start()]) + "[redacted private key]\n"
        return redact(line)

    def feed(self, chunk):
        self.pending += chunk
        out = []
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if self.dropping:
                self.dropping = False
            else:
                out.append(self._line(line + "\n"))
        # Refuse to retain or emit an unbounded, possibly secret-bearing line.
        if len(self.pending) > 65536:
            self.pending = ""
            self.dropping = True
            self.in_key = True  # an overlong line could contain a partial PEM delimiter
            out.append("[redacted overlong output]\n")
        return "".join(out)

    def finish(self):
        tail, self.pending = self.pending, ""
        return "" if self.dropping else self._line(tail)
