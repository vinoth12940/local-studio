"""The chat harness approval gate for action tools.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import json
import re

from studio.infra.edits import _make_diff_preview, _match_edit_block
from studio.infra.harness_args import _normalize_harness_args
from studio.infra.workspace import _harness_root
from studio.security.fences import _gate_shell_command, _harness_path_fence, _is_safe_readonly_shell
from studio.services.mcp_custom import _get_mcp_chat_tools
from studio.storage.settings import load_settings


_chat_session_perms: dict[str, set[str]] = {}                # chat_id -> {"edit", "exec", "mcp"} granted for the session


def harness_gate(name, args, chat_id=None, extra_perms=()):
    """Classify a chat harness tool call as ('safe'|'risky'|'blocked', reason, preview_meta).
    extra_perms are granted for this request only (the composer's "Accept edits" mode sends {"edit"})."""
    args = _normalize_harness_args(name, args)
    if not isinstance(args, dict):
        return "blocked", "Tool arguments must be a JSON object.", None
    perms = (_chat_session_perms.get(chat_id, set()) if chat_id else set()) | set(extra_perms)
    if name.startswith("mcp__"):
        return _gate_custom_mcp_tool(name, args, perms)
    if name == "write_file":
        try:
            target = _harness_path_fence(args.get("path"), write=True)
        except ValueError as err:
            return "blocked", str(err), None
        content = args.get("content")
        if not isinstance(content, str):
            return "blocked", "write_file requires a string 'content' argument.", None
        if len(content) > 500_000:
            return "blocked", "File content exceeds the 500 KB write limit.", None
        old_text = ""
        if target.exists():
            if target.is_dir() or target.is_symlink():
                return "blocked", "Target path is a directory or symlink.", None
            try:
                old_text = target.read_text(encoding="utf-8", errors="replace")
            except OSError as err:
                return "blocked", str(err), None
        preview = _make_diff_preview(str(target), old_text, content) if old_text else "\n".join(f"+{ln}" for ln in content.splitlines()[:60])[:4000]
        action_word = "Overwrite" if target.exists() else "Create"
        meta = {"title": f"{action_word} {target.name}", "path": str(target), "preview": preview, "kind": "diff", "perm_scope": "edit"}
        if "edit" in perms:
            return "safe", None, meta
        return "risky", f"{action_word} local file {target}", meta

    if name == "edit_file":
        try:
            target = _harness_path_fence(args.get("path"), write=True)
        except ValueError as err:
            return "blocked", str(err), None
        if not target.is_file() or target.is_symlink():
            return "blocked", f"File not found: {target}", None
        old_s, new_s = args.get("old_text"), args.get("new_text")
        if not isinstance(old_s, str) or not old_s or not isinstance(new_s, str):
            return "blocked", "edit_file requires non-empty 'old_text' and string 'new_text'.", None
        try:
            current = target.read_text(encoding="utf-8", errors="replace")
            count, updated = _match_edit_block(current, old_s, new_s, bool(args.get("replace_all")))
        except (OSError, ValueError) as err:
            return "blocked", str(err), None
        preview = _make_diff_preview(str(target), current, updated)
        meta = {"title": f"Edit {target.name}", "path": str(target), "preview": preview, "kind": "diff", "perm_scope": "edit"}
        if "edit" in perms:
            return "safe", None, meta
        return "risky", f"Modify {target} ({count} replacement{'s' if count > 1 else ''})", meta

    if name == "multi_edit_file":
        try:
            target = _harness_path_fence(args.get("path"), write=True)
        except ValueError as err:
            return "blocked", str(err), None
        if not target.is_file() or target.is_symlink():
            return "blocked", f"File not found: {target}", None
        edits = args.get("edits")
        if not isinstance(edits, list) or not edits:
            return "blocked", "multi_edit_file requires a non-empty 'edits' list of {old_text, new_text}.", None
        try:
            current = target.read_text(encoding="utf-8", errors="replace")
            updated = current
            total_rep = 0
            for idx, ed in enumerate(edits, 1):
                if not isinstance(ed, dict):
                    return "blocked", f"Edit #{idx} must be an object with old_text and new_text.", None
                old_s, new_s = ed.get("old_text"), ed.get("new_text")
                if not isinstance(old_s, str) or not old_s or not isinstance(new_s, str):
                    return "blocked", f"Edit #{idx} requires non-empty 'old_text' and string 'new_text'.", None
                c, updated = _match_edit_block(updated, old_s, new_s, bool(ed.get("replace_all")))
                total_rep += c
        except (OSError, ValueError) as err:
            return "blocked", str(err), None
        preview = _make_diff_preview(str(target), current, updated)
        meta = {"title": f"Multi-edit {target.name} ({len(edits)} blocks)", "path": str(target), "preview": preview, "kind": "diff", "perm_scope": "edit"}
        if "edit" in perms:
            return "safe", None, meta
        return "risky", f"Modify {target} ({total_rep} replacement{'s' if total_rep > 1 else ''} across {len(edits)} blocks)", meta

    if name == "run_python":
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return "blocked", "run_python requires a non-empty 'code' string.", None
        if len(code) > 50_000 or "\x00" in code:
            return "blocked", "Python snippet is too large or invalid.", None
        if re.search(r"(?:\.ssh|\.aws|\.gnupg|Keychains)", code, re.I):
            return "blocked", "Accessing credential or keychain directories in run_python is blocked.", None
        meta = {"title": "Run Python snippet", "preview": code.strip()[:3000], "kind": "code", "perm_scope": "exec"}
        if "exec" in perms:
            return "safe", None, meta
        return "risky", "Executes Python 3 code locally on this Mac.", meta

    if name == "run_shell":
        cmd = args.get("command")
        ok, reason = _gate_shell_command(cmd)
        if not ok:
            return "blocked", reason, None
        try:
            cwd = _harness_path_fence(args.get("cwd"), write=False) if args.get("cwd") else _harness_root()
        except ValueError as err:
            return "blocked", str(err), None
        if not cwd.is_dir():
            return "blocked", f"Working directory does not exist: {cwd}", None
        bg = bool(args.get("background"))
        meta = {"title": f"Run {'background ' if bg else ''}shell command in {cwd.name or str(cwd)}",
                "preview": f"$ {cmd.strip()}", "kind": "shell", "path": str(cwd), "perm_scope": "exec"}
        if "exec" in perms:
            return "safe", None, meta
        if not bg and load_settings().get("harness", {}).get("auto_approve_readonly", True) and _is_safe_readonly_shell(cmd):
            return "safe", None, {**meta, "readonly_allowed": True}
        return "risky", f"Runs command in {cwd}", meta

    return "safe", None, None


def _gate_custom_mcp_tool(full_name: str, args: dict, perms: set) -> tuple[str, str | None, dict | None]:
    _, meta_map = _get_mcp_chat_tools()
    info = meta_map.get(full_name)
    if not info:
        parts = full_name.split("__", 2)
        srv_id, tool_name = (parts[1], parts[2]) if len(parts) == 3 else ("custom", full_name)
        info = {"server_id": srv_id, "server_name": srv_id, "name": tool_name, "trusted": False}
    preview = json.dumps(args or {}, indent=2, ensure_ascii=False)[:2000]
    meta = {
        "title": f"MCP {info['server_name']}: {info['name']}",
        "preview": preview, "kind": "code", "perm_scope": "mcp"
    }
    if info.get("trusted") or "mcp" in perms:
        return "safe", None, meta
    return "risky", f"Calls external MCP server '{info['server_name']}' tool '{info['name']}'", meta
