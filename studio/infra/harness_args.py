"""Repair and normalization of tool-call arguments written by local models.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import ast
import json
import re


def _parse_harness_args(raw):
    """Parse tool arguments from local models, repairing code fences or trailing commas when needed."""
    parsed = _call_args(raw)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I | re.S).strip()
        repaired = re.sub(r",\s*([}\]])", r"\1", s)
        for candidate in (s, repaired):
            try:
                val = json.loads(candidate)
                if isinstance(val, dict):
                    return val
            except ValueError:
                pass
        try:
            val = ast.literal_eval(s)
            if isinstance(val, dict):
                return val
        except Exception:
            pass
    return parsed


def _normalize_harness_args(name, args):
    """Map common parameter aliases and stringified integers emitted by local models to canonical schema keys."""
    if not isinstance(args, dict):
        return args
    out = dict(args)
    alias_map = {
        "read_file": (("path", ("filepath", "file_path", "file", "filename")), ("file_id", ("id",)),
                      ("start_line", ("line", "from_line", "offset")), ("max_lines", ("lines", "limit", "n_lines"))),
        "write_file": (("path", ("filepath", "file_path", "file", "filename", "target_file")),
                       ("content", ("text", "body", "code", "contents"))),
        "edit_file": (("path", ("filepath", "file_path", "file", "filename", "target_file")),
                      ("old_text", ("old_str", "old_string", "search", "target", "find", "old", "old_content")),
                      ("new_text", ("new_str", "new_string", "replace", "replacement", "new", "new_content"))),
        "multi_edit_file": (("path", ("filepath", "file_path", "file", "filename", "target_file")),
                            ("edits", ("changes", "replacements", "patches", "chunks"))),
        "code_outline": (("path", ("filepath", "file_path", "file", "filename")),),
        "git_status": (("path", ("dir", "directory", "folder", "cwd")),),
        "git_diff": (("path", ("filepath", "file_path", "file", "dir")),),
        "delegate_task": (("task", ("prompt", "query", "instruction", "description", "goal")),
                          ("role", ("agent", "type", "worker", "subagent"))),
        "manage_process": (("process_id", ("pid", "id", "proc_id")), ("action", ("cmd", "command", "op"))),
        "list_directory": (("path", ("dir", "directory", "folder", "dirpath")),),
        "find_files": (("pattern", ("query", "glob", "name", "filename")), ("path", ("dir", "directory", "folder"))),
        "grep_files": (("query", ("pattern", "search", "text", "regex")), ("path", ("dir", "directory", "folder", "file")),
                       ("glob", ("file_pattern", "include", "filter"))),
        "run_python": (("code", ("script", "python", "source", "program")),),
        "run_shell": (("command", ("cmd", "shell", "script")), ("cwd", ("dir", "directory", "path", "workdir"))),
        "todo_write": (("todos", ("tasks", "items", "plan", "steps", "checklist")),),
        "memory_store": (("key", ("name", "topic", "title")), ("value", ("content", "text", "fact", "memory"))),
        "memory_recall": (("query", ("key", "search", "topic", "q")),),
        "ask_user": (("question", ("prompt", "text", "message", "query")), ("options", ("choices", "items"))),
        "web_search": (("query", ("q", "search", "keywords")),),
        "read_webpage": (("url", ("link", "href", "page")),),
        "search_files": (("query", ("q", "search", "name")),),
    }
    for canonical, aliases in alias_map.get(name, ()):
        if out.get(canonical) is None:
            for alt in aliases:
                if out.get(alt) is not None:
                    out[canonical] = out.pop(alt)
                    break
    for int_key in ("file_id", "start_page", "max_pages", "start_line", "max_lines", "timeout"):
        val = out.get(int_key)
        if isinstance(val, str) and val.strip().lstrip("-").isdigit():
            out[int_key] = int(val.strip())
    if name == "read_file":
        if type(out.get("start_page")) is int:
            out["start_page"] = max(1, out["start_page"])
        if type(out.get("max_pages")) is int:
            out["max_pages"] = max(1, min(4, out["max_pages"]))
    if name == "multi_edit_file" and isinstance(out.get("edits"), list):
        norm_edits = []
        for ed in out["edits"]:
            if isinstance(ed, dict):
                norm_edits.append(_normalize_harness_args("edit_file", ed))
        out["edits"] = norm_edits
    if name == "todo_write" and isinstance(out.get("todos"), list):
        norm_todos = []
        for item in out["todos"][:25]:
            if isinstance(item, str) and item.strip():
                norm_todos.append({"content": item.strip()[:240], "status": "pending"})
            elif isinstance(item, dict):
                content = str(item.get("content") or item.get("task") or item.get("title") or item.get("text") or "").strip()[:240]
                raw_st = str(item.get("status") or ("completed" if item.get("done") else "pending")).strip().lower()
                st = "completed" if raw_st in ("completed", "done", "complete", "finished", "true") else \
                     "in_progress" if raw_st in ("in_progress", "active", "running", "doing", "working") else "pending"
                if content:
                    norm_todos.append({"content": content, "status": st})
        out["todos"] = norm_todos
    return out


def _call_args(a):
    """Arguments as the model wrote them: a dict, or JSON text of one. Anything else is handed back unchanged for validate_call."""
    if isinstance(a, str):
        if not a.strip():
            return {}
        try:
            a = json.loads(a)
        except ValueError:
            return a
        if isinstance(a, str):                               # JSON text inside JSON text
            try:
                a = json.loads(a)
            except ValueError:
                pass
    return {} if a is None else a
