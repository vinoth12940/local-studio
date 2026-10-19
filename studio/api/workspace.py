"""Routes: workspace.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import fnmatch
import os
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from studio.services import harness as services_harness
from studio.infra.edits import _post_edit_diagnostics
from studio.infra.workspace import _harness_root, _in_workspace
from studio.security.fences import (
    _gate_shell_command, _harness_path_fence, _HARNESS_SECRET_GLOBS, _HARNESS_SENSITIVE_NAMES, _HARNESS_SKIP_DIRS,
)
from studio.services.harness import (
    _chat_action_tool, _code_outline, _load_workspace_instructions, _WORKSPACE_INSTRUCTION_FILES,
)
from studio.storage.settings import load_settings, save_settings

router = APIRouter()


# -- Workspace @file / @folder Mentions & Slash Commands (Tier 1.2 & Tier 3.3)

@router.get("/api/workspace/files")
def api_workspace_files(workspace: str | None = None, q: str = "", limit: int = 25):
    """Fuzzy-search workspace files and folders for @file and @folder autocomplete in the chat composer."""
    try:
        base = _harness_path_fence(workspace, write=False) if workspace else _harness_root()
    except ValueError as err:
        raise HTTPException(400, str(err))
    if not base.is_dir():
        raise HTTPException(400, f"Workspace directory not found: {base}")
    q_low = (q or "").strip().lower().lstrip("@")
    cap = max(1, min(int(limit or 25), 100))
    candidates = []
    seen = 0
    for root_dir, dirs, files in os.walk(base):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".") and d not in _HARNESS_SKIP_DIRS and d not in _HARNESS_SENSITIVE_NAMES and not d.lower().endswith(".app")]
        root_p = Path(root_dir)
        for d in dirs:
            full_d = root_p / d
            rel_d = str(full_d.relative_to(base))
            candidates.append({"path": rel_d, "rel_path": rel_d + "/", "abs_path": str(full_d), "name": d + "/", "type": "dir", "size": None})
        for fname in sorted(files):
            if fname.startswith(".") and fname not in _WORKSPACE_INSTRUCTION_FILES:
                continue
            if any(fnmatch.fnmatch(fname.lower(), g) for g in _HARNESS_SECRET_GLOBS):
                continue
            full_f = root_p / fname
            rel_f = str(full_f.relative_to(base))
            try:
                sz = full_f.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            candidates.append({"path": rel_f, "rel_path": rel_f, "abs_path": str(full_f), "name": fname, "type": "file", "size": sz})
            seen += 1
            if seen >= 2500:
                break
        if seen >= 2500:
            break

    def _score(item):
        name_l = item["name"].lower().rstrip("/")
        rel_l = item["rel_path"].lower().rstrip("/")
        if not q_low:
            return (0 if item["type"] == "file" else 1, rel_l.count("/"), rel_l)
        if name_l == q_low or rel_l == q_low:
            return (0, len(rel_l), rel_l)
        if name_l.startswith(q_low):
            return (1, len(rel_l), rel_l)
        if q_low in name_l:
            return (2, len(rel_l), rel_l)
        if q_low in rel_l:
            return (3, len(rel_l), rel_l)
        # Subsequence fuzzy match
        it = iter(rel_l)
        if all(ch in it for ch in q_low):
            return (4, len(rel_l), rel_l)
        return None

    scored = []
    for item in candidates:
        sc = _score(item)
        if sc is not None:
            scored.append((sc, item))
    scored.sort(key=lambda pair: pair[0])
    return {"workspace": str(base), "items": [pair[1] for pair in scored[:cap]]}


@router.get("/api/workspace/info")
def api_workspace_info(workspace: str | None = None):
    """Return workspace summary for the Code page: loaded AGENTS.md/CLAUDE.md files, git status/diff, and harness settings."""
    try:
        base = _harness_path_fence(workspace, write=False) if workspace else _harness_root()
    except ValueError as err:
        raise HTTPException(400, str(err))
    if not base.is_dir():
        raise HTTPException(400, f"Workspace directory not found: {base}")
    ws_str = str(base)
    inst = _load_workspace_instructions(ws_str)
    instructions = [
        {"name": item["name"], "path": item["path"], "chars": len(item["content"]), "preview": item["content"][:600]}
        for item in inst
    ]
    try:
        git_st = _in_workspace(ws_str, services_harness._chat_read_tool, "git_status", {"path": ws_str})
    except Exception as err:
        git_st = {"error": str(err)}
    try:
        git_df = _in_workspace(ws_str, services_harness._chat_read_tool, "git_diff", {"path": ws_str})
    except Exception as err:
        git_df = {"error": str(err)}
    s = load_settings()
    return {
        "workspace": ws_str,
        "instructions": instructions,
        "git_status": git_st,
        "git_diff": git_df,
        "auto_approve_readonly": bool(s.get("agent", {}).get("auto_approve_readonly", True)),
    }


@router.get("/api/workspace/outline")
def api_workspace_outline(path: str, workspace: str | None = None):
    """Return symbol outline (classes, functions, methods, sections) for a file in the workspace."""
    try:
        ws_str = str(_harness_path_fence(workspace, write=False)) if workspace else str(_harness_root())
        return _in_workspace(ws_str, _code_outline, path)
    except Exception as err:
        raise HTTPException(400, str(err))


@router.post("/api/workspace/settings")
def api_workspace_settings(body: dict = Body(default={})):
    """Toggle harness coding settings such as auto_approve_readonly."""
    s = load_settings()
    ag = s.setdefault("agent", {})
    if "auto_approve_readonly" in body:
        ag["auto_approve_readonly"] = bool(body["auto_approve_readonly"])
    save_settings(s)
    return {"ok": True, "auto_approve_readonly": ag.get("auto_approve_readonly", True)}


@router.get("/api/workspace/file")
def api_workspace_read_file(path: str, workspace: str | None = None):
    """Read a workspace file for the Code Editor along with its syntax diagnostics and git diff."""
    try:
        ws_str = str(_harness_path_fence(workspace, write=False)) if workspace else str(_harness_root())
        target = _in_workspace(ws_str, _harness_path_fence, path, False)
    except ValueError as err:
        raise HTTPException(400, str(err))
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"File not found: {path}")
    try:
        raw = target.read_bytes()
    except OSError as err:
        raise HTTPException(400, str(err))
    if len(raw) > 512_000:
        raise HTTPException(400, "File is too large for editor preview (>500 KB)")
    if b"\x00" in raw[:4096]:
        raise HTTPException(400, "Binary file cannot be opened in text editor")
    content = raw.decode("utf-8", errors="replace")
    diag = _post_edit_diagnostics(target)
    try:
        df = _in_workspace(ws_str, services_harness._chat_read_tool, "git_diff", {"path": str(target)})
    except Exception:
        df = {"diff": ""}
    return {
        "path": path,
        "abs_path": str(target),
        "name": target.name,
        "content": content,
        "lines": len(content.splitlines()),
        "syntax_check": diag.get("syntax_check", "skipped"),
        "syntax_ok": diag.get("syntax_ok", True),
        "diff": df.get("diff", ""),
    }


@router.post("/api/workspace/file")
def api_workspace_save_file(body: dict = Body(default={})):
    """Save edits from the Code Editor, create an undo backup, and run automatic syntax/lint diagnostics."""
    path = (body.get("path") or "").strip()
    workspace = body.get("workspace")
    content = body.get("content")
    if not path or not isinstance(content, str):
        raise HTTPException(400, "path and content are required")
    try:
        ws_str = str(_harness_path_fence(workspace, write=False)) if workspace else str(_harness_root())
        res = _in_workspace(ws_str, _chat_action_tool, "write_file", {"path": path, "content": content})
        return res
    except Exception as err:
        raise HTTPException(400, str(err))


@router.post("/api/workspace/exec")
def api_workspace_exec(body: dict = Body(default={})):
    """Run a shell command in the active workspace from the Code view's integrated terminal."""
    cmd = (body.get("command") or "").strip()
    workspace = body.get("workspace")
    background = bool(body.get("background", False))
    if not cmd:
        raise HTTPException(400, "command is required")
    ok, reason = _gate_shell_command(cmd)
    if not ok:
        raise HTTPException(400, reason)
    try:
        ws_str = str(_harness_path_fence(workspace, write=False)) if workspace else str(_harness_root())
        return _in_workspace(ws_str, _chat_action_tool, "run_shell", {"command": cmd, "background": background, "timeout": 30})
    except Exception as err:
        raise HTTPException(400, str(err))
