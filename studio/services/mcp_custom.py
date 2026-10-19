"""Custom MCP servers: clients, tool discovery and calls.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import re
import threading
import uuid

from studio.infra import mcp_client
from studio.infra.mcp_client import McpError
from studio.storage.settings import load_settings


# -- Custom MCP Server Registry & Dynamic Tool Bridging (Tier 2.1)

_custom_mcp_lock = threading.Lock()
_custom_mcp_clients: dict[str, "McpClient"] = {}
_custom_mcp_tools_cache: dict[str, list[dict]] = {}


def _sanitize_mcp_id(raw_id: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", str(raw_id or "").strip().lower()).strip("_")
    return clean[:32] or ("srv_" + uuid.uuid4().hex[:6])


def _get_custom_mcp_client(srv: dict) -> "McpClient":
    sid = srv["id"]
    cmd = str(srv.get("command") or "").strip()
    if not cmd:
        raise McpError(f"MCP server '{srv.get('name', sid)}' has no command configured.")
    argv = [cmd] + [str(a) for a in (srv.get("args") or [])]
    env = {str(k): str(v) for k, v in (srv.get("env") or {}).items()} if isinstance(srv.get("env"), dict) else None
    with _custom_mcp_lock:
        cli = _custom_mcp_clients.get(sid)
        if cli is None or cli.argv != argv or getattr(cli, "env", None) != env:
            if cli is not None:
                cli.close()
            cli = mcp_client.McpClient(argv, allowed=None, env=env)
            _custom_mcp_clients[sid] = cli
        return cli


def _discover_server_tools(srv: dict, force=False) -> list[dict]:
    sid = srv["id"]
    if not force and sid in _custom_mcp_tools_cache:
        return _custom_mcp_tools_cache[sid]
    cli = _get_custom_mcp_client(srv)
    raw_tools = cli.list_tools(timeout=15)
    norm = []
    for t in raw_tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        tname = str(t["name"]).strip()
        schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object", "properties": {}}
        norm.append({
            "name": tname,
            "full_name": f"mcp__{sid}__{tname}",
            "description": str(t.get("description") or f"MCP tool {tname} from {srv.get('name', sid)}"),
            "inputSchema": schema,
            "server_id": sid,
            "server_name": srv.get("name") or sid,
            "trusted": bool(srv.get("trusted"))
        })
    _custom_mcp_tools_cache[sid] = norm
    return norm


def _get_mcp_chat_tools() -> tuple[list[dict], dict[str, dict]]:
    """Return (openai_tool_schemas, {full_name: tool_meta}) for all enabled custom MCP servers."""
    servers = load_settings().get("mcp_servers") or []
    schemas, meta_map = [], {}
    for srv in servers:
        if not isinstance(srv, dict) or not srv.get("enabled", True) or not srv.get("id"):
            continue
        try:
            tools = _discover_server_tools(srv, force=False)
        except Exception:
            continue
        for t in tools:
            fn_name = t["full_name"]
            params = dict(t["inputSchema"]) if isinstance(t.get("inputSchema"), dict) else {"type": "object", "properties": {}}
            params.setdefault("type", "object")
            params.setdefault("properties", {})
            schemas.append({"type": "function", "function": {
                "name": fn_name,
                "description": f"[{t['server_name']} MCP] {t['description']}"[:500],
                "parameters": params
            }})
            meta_map[fn_name] = t
    return schemas, meta_map


def _call_custom_mcp_tool(full_name: str, args: dict) -> dict:
    parts = full_name.split("__", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid MCP tool name: {full_name}")
    _, sid, tool_name = parts
    servers = load_settings().get("mcp_servers") or []
    srv = next((s for s in servers if isinstance(s, dict) and s.get("id") == sid), None)
    if not srv:
        raise ValueError(f"MCP server '{sid}' is not configured.")
    cli = _get_custom_mcp_client(srv)
    res = cli.call(tool_name, args or {}, timeout=30)
    if not res.get("ok"):
        return {"error": res.get("text") or f"MCP tool {tool_name} failed", "verified": False}
    return {"server": srv.get("name") or sid, "tool": tool_name, "ok": True,
            "result": res.get("data") if res.get("data") is not None else (res.get("text") or "")[:8000]}
