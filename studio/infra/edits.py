"""Edit matching, diff previews and post-edit syntax diagnostics for the harness.

Moved from app.py in the refactor's P1 split.
"""

import ast
import difflib
import json
import re
import shutil
import subprocess
from pathlib import Path


def _post_edit_diagnostics(target: Path) -> dict:
    """Run fast, deterministic syntax/lint verification on a modified file so the model can self-heal in the next round."""
    ext = target.suffix.lower()
    try:
        if ext == ".py":
            src = target.read_text(encoding="utf-8", errors="replace")
            try:
                ast.parse(src, filename=str(target))
                return {"syntax_check": "OK", "syntax_ok": True, "checker": "ast.parse"}
            except SyntaxError as err:
                msg = f"SyntaxError at line {err.lineno}: {err.msg}"
                return {"syntax_check": msg, "syntax_ok": False, "checker": "ast.parse",
                        "line": err.lineno, "offset": err.offset, "error": msg}
        if ext == ".json":
            src = target.read_text(encoding="utf-8", errors="replace")
            try:
                json.loads(src)
                return {"syntax_check": "OK", "syntax_ok": True, "checker": "json.loads"}
            except json.JSONDecodeError as err:
                msg = f"JSONDecodeError at line {err.lineno}, column {err.colno}: {err.msg}"
                return {"syntax_check": msg, "syntax_ok": False, "checker": "json.loads",
                        "line": err.lineno, "column": err.colno, "error": msg}
        if ext in (".js", ".mjs", ".cjs") and shutil.which("node"):
            res = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True, timeout=4)
            if res.returncode == 0:
                return {"syntax_check": "OK", "syntax_ok": True, "checker": "node --check"}
            err_line = "\n".join((res.stderr or res.stdout or "Syntax error").strip().splitlines()[:4])
            return {"syntax_check": err_line[:400], "syntax_ok": False, "checker": "node --check", "error": err_line[:400]}
        if ext == ".swift" and shutil.which("swiftc"):
            res = subprocess.run(["swiftc", "-parse", str(target)], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                return {"syntax_check": "OK", "syntax_ok": True, "checker": "swiftc -parse"}
            err_line = "\n".join((res.stderr or res.stdout or "Swift syntax error").strip().splitlines()[:4])
            return {"syntax_check": err_line[:400], "syntax_ok": False, "checker": "swiftc -parse", "error": err_line[:400]}
    except Exception as exc:
        return {"syntax_check": f"skipped ({exc})", "syntax_ok": True}
    return {"syntax_check": "OK", "syntax_ok": True, "checker": "none"}


def _make_diff_preview(path_str, old_content, new_content, max_lines=80):
    diff = list(difflib.unified_diff(
        old_content.splitlines(), new_content.splitlines(),
        fromfile=f"a/{Path(path_str).name}", tofile=f"b/{Path(path_str).name}", lineterm=""
    ))
    if not diff:
        return "(no text changes)"
    text = "\n".join(diff[:max_lines])
    if len(diff) > max_lines:
        text += f"\n... (+{len(diff) - max_lines} more diff lines)"
    return text[:4000]


_LINE_NO_RE = re.compile(r"^\s*\d+: ?")


def _strip_line_numbers(text):
    """Drop `12: ` prefixes a model copied from read_file output, only when every non-empty line has one."""
    lines = text.split("\n")
    if not any(ln.strip() for ln in lines) or not all(_LINE_NO_RE.match(ln) for ln in lines if ln.strip()):
        return text
    return "\n".join(_LINE_NO_RE.sub("", ln, count=1) for ln in lines)


def _match_edit_block(current, old_s, new_s, replace_all=False):
    """(replacements, updated text) for an edit_file call; raises ValueError with guidance when it can't place it.
    Tries the exact text, then the text without copied line numbers, then the same lines ignoring indentation
    and trailing spaces (the replacement is re-indented to match the file). Never edits an ambiguous match."""
    def exact(old, new):
        count = current.count(old)
        if count > 1 and not replace_all:
            raise ValueError(f"old_text matched {count} places. Include more surrounding lines or set replace_all=true.")
        return (count if replace_all else 1, current.replace(old, new) if replace_all else current.replace(old, new, 1)) if count else None

    hit = exact(old_s, new_s)
    if hit:
        return hit
    old2 = _strip_line_numbers(old_s)
    new2 = _strip_line_numbers(new_s) if old2 != old_s else new_s
    if old2 != old_s and old2.strip():
        hit = exact(old2, new2)
        if hit:
            return hit
    want = [ln.strip() for ln in old2.strip("\n").split("\n")]
    if any(want):
        lines = current.splitlines(keepends=True)
        n = len(want)
        spots = [i for i in range(len(lines) - n + 1) if [ln.strip() for ln in lines[i:i + n]] == want]
        if len(spots) > 1 and not replace_all:
            raise ValueError(f"old_text matched {len(spots)} places (ignoring indentation). Include more surrounding lines or set replace_all=true.")
        if spots:
            first_old = next(ln for ln in old2.strip("\n").split("\n") if ln.strip())
            old_indent = first_old[:len(first_old) - len(first_old.lstrip())]
            new_lines = new2.strip("\n").split("\n") if new2.strip("\n") else []
            for i in reversed(spots if replace_all else spots[:1]):
                file_first = next(ln for ln in lines[i:i + n] if ln.strip())
                file_indent = file_first[:len(file_first) - len(file_first.lstrip())]
                fixed = [(file_indent + ln[len(old_indent):]) if ln.startswith(old_indent) else (file_indent + ln.lstrip() if ln.strip() else ln)
                         for ln in new_lines]
                end = lines[i + n - 1]
                eol = end[len(end.rstrip("\r\n")):] or ("\n" if i + n < len(lines) else "")
                lines[i:i + n] = [ln + "\n" for ln in fixed[:-1]] + ([fixed[-1] + eol] if fixed else [])
            return (len(spots) if replace_all else 1, "".join(lines))
    raise ValueError("old_text was not found in the file. Call read_file with path first and copy the exact lines (without line numbers).")
