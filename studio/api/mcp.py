"""Routes: mcp.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import shlex

from fastapi import APIRouter, Body, HTTPException

from studio.services.mcp_custom import (
    _custom_mcp_clients, _custom_mcp_lock, _custom_mcp_tools_cache, _discover_server_tools, _sanitize_mcp_id,
    _mcp_fingerprint, _mcp_launch_preview,
)
from studio.storage.settings import load_settings, save_settings

router = APIRouter()


@router.get("/api/mcp/servers")
def api_get_mcp_servers():
    servers = load_settings().get("mcp_servers") or []
    out = []
    for srv in servers:
        if not isinstance(srv, dict) or not srv.get("id"):
            continue
        sid = srv["id"]
        cached = _custom_mcp_tools_cache.get(sid, [])
        cli = _custom_mcp_clients.get(sid)
        out.append({
            **srv,
            "launch_preview": _mcp_launch_preview(srv),
            "launch_fingerprint": _mcp_fingerprint(srv),
            "running": bool(cli and cli.alive),
            "tools_count": len(cached),
            "tools": [{"name": t["name"], "description": t["description"]} for t in cached]
        })
    return {"servers": out}


@router.post("/api/mcp/servers")
def api_save_mcp_server(body: dict = Body(...)):
    name = str(body.get("name") or body.get("id") or "").strip()[:80]
    command = str(body.get("command") or "").strip()
    if not name or not command:
        raise HTTPException(400, "MCP server 'name' and 'command' are required.")
    raw_args = body.get("args") or []
    if isinstance(raw_args, str):
        try:
            raw_args = shlex.split(raw_args)
        except ValueError:
            raw_args = raw_args.split()
    args_list = [str(a) for a in raw_args if str(a).strip()]
    env_dict = {str(k): str(v) for k, v in (body.get("env") or {}).items()} if isinstance(body.get("env"), dict) else {}
    sid = _sanitize_mcp_id(body.get("id") or name)
    entry = {
        "id": sid, "name": name, "command": command, "args": args_list,
        "env": env_dict, "enabled": bool(body.get("enabled", True)),
        "trusted": bool(body.get("trusted", False))
    }
    s = load_settings()
    servers = [x for x in (s.get("mcp_servers") or []) if isinstance(x, dict)]
    idx = next((i for i, x in enumerate(servers) if x.get("id") == sid), None)
    if idx is not None:
        if servers[idx].get("launch_approved") == _mcp_fingerprint(entry):
            entry["launch_approved"] = servers[idx]["launch_approved"]
        servers[idx] = entry
    else:
        servers.append(entry)
    s["mcp_servers"] = servers
    save_settings(s)
    _custom_mcp_tools_cache.pop(sid, None)
    discovered = []
    error_msg = None
    if entry["enabled"] and body.get("discover", False):
        try:
            discovered = _discover_server_tools(entry, force=True)
        except Exception as exc:
            error_msg = str(exc)
    return {"ok": True, "server": entry, "tools": [{"name": t["name"], "description": t["description"]} for t in discovered],
            "error": error_msg}


@router.delete("/api/mcp/servers/{server_id}")
def api_delete_mcp_server(server_id: str):
    s = load_settings()
    s["mcp_servers"] = [x for x in (s.get("mcp_servers") or []) if isinstance(x, dict) and x.get("id") != server_id]
    save_settings(s)
    _custom_mcp_tools_cache.pop(server_id, None)
    with _custom_mcp_lock:
        cli = _custom_mcp_clients.pop(server_id, None)
        if cli is not None:
            cli.close()
    return {"ok": True}


@router.post("/api/mcp/servers/{server_id}/test")
def api_test_mcp_server(server_id: str, body: dict = Body(default={})):
    servers = load_settings().get("mcp_servers") or []
    srv = next((x for x in servers if isinstance(x, dict) and x.get("id") == server_id), None)
    if not srv:
        raise HTTPException(404, f"MCP server '{server_id}' not found.")
    if body.get("approve_launch") == _mcp_fingerprint(srv):
        srv["launch_approved"] = _mcp_fingerprint(srv)
        s = load_settings()
        s["mcp_servers"] = [srv if x.get("id") == server_id else x for x in s.get("mcp_servers", [])]
        save_settings(s)
    try:
        tools = _discover_server_tools(srv, force=True)
        return {"ok": True, "server_id": server_id, "tools_count": len(tools),
                "tools": [{"name": t["name"], "description": t["description"]} for t in tools]}
    except Exception as exc:
        raise HTTPException(502, f"MCP server test failed: {exc}")
