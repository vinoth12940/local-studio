"""Patch-point inventory: every module global of `app` that the check suites rebind or mutate, with the app.py section
that owns it today, the module it moves to in the studio/ split, and the other sections that read it.

    python3 -I macos/inventory_patches.py            # print the inventory as JSON
    python3 -I macos/inventory_patches.py --write    # rewrite macos/fixtures/patch_points.json

A test that patches `app.X` only tests something while the code reads `X` from the module the test patched. A name that is
*rebound* (`app.X = fake`) must be read through its owning module after the split (`engines.ensure_engine()`, never
`from ... import ensure_engine`). A name that is only *mutated* (`app.jobs.clear()`) is one shared object and survives any
import style. `macos/check_app.py` (patch_points_are_inventoried) fails when this inventory and the fixture disagree.
"""
import ast
import bisect
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "macos" / "fixtures" / "patch_points.json"
MODULE_OBJECTS = {"subprocess", "httpx", "socket", "os", "time", "shutil", "asyncio", "threading"}
MUTATORS = {"clear", "update", "pop", "append", "setdefault", "extend", "remove", "add", "discard", "insert"}

# Where each name moves in the P1 split (docs/prompts/modularization-performance-security-prompt-v2.md §5 P1). Banners in
# app.py are not owners: the harness lives under the "LLM proxy" and "terminal" banners, the agent loop under "approval gate".
DESTINATION = {
    "SEARCH_URL": "studio.config", "CONTEXT": "studio.config", "AGENT_SOCK": "studio.config", "AGENT_RUNS": "studio.config",
    "CUA_DRIVER": "studio.config", "agent_engine": "studio.config",
    "_download": "studio.services.models", "downloads": "studio.services.models", "model_installed": "studio.services.models",
    "_engine_ready": "studio.services.engines", "_stop_engine": "studio.services.engines", "native_context": "studio.services.models",
    "engine_state": "studio.services.engines", "ensure_engine": "studio.services.engines",
    "_chat_sessions": "studio.services.harness", "_chat_read_tool": "studio.services.harness",
    "_fetch_public_web": "studio.services.web",
    "jobs": "studio.services.images",
    "_enable_search": "studio.services.search", "_search_process": "studio.services.search", "_search_proxy": "studio.services.search",
    "_search_running": "studio.services.search", "_start_search": "studio.services.search", "search_job": "studio.services.search",
    "AGENT_SOCK_WAIT_S": "studio.services.agent.cua", "McpClient": "studio.infra.mcp_client", "_agent_cli": "studio.services.agent.cua",
    "_agent_daemon_running": "studio.services.agent.cua", "_agent_last_use": "studio.services.agent.cua",
    "_agent_reaper_start": "studio.services.agent.cua", "_cua_perm": "studio.services.agent.cua", "cua_status": "studio.services.agent.cua",
    "start_agent_daemon": "studio.services.agent.cua", "stop_agent_daemon": "studio.services.agent.cua",
    "app_view": "studio.services.agent.views", "browser_view": "studio.services.agent.views",
    "gate": "studio.security.agent_gate", "AGENT_PROFILE": "studio.services.agent.loop",
    "AGENT_SEARCH_SECONDS": "studio.services.agent.loop", "AGENT_TOOLS": "studio.services.agent.loop",
    "_agent_adapter": "studio.services.agent.loop", "_agent_client": "studio.services.agent.loop",
    "_agent_current": "studio.services.agent.loop", "_agent_engine_current": "studio.services.agent.loop",
    "_agent_model": "studio.services.agent.loop", "_agent_now": "studio.services.agent.loop",
    "_agent_profile_in_use": "studio.services.agent.loop", "_agent_runs": "studio.services.agent.loop",
    "_agent_sleep": "studio.services.agent.loop", "_cua_cache": "studio.services.agent.loop",
    # rebound with `global` in app code (not patched by the suites, but a copy would go stale the same way)
    "active_chats": "studio.services.harness", "_agent_reaper": "studio.services.agent.cua", "_cua_grant": "studio.services.agent.loop",
}
REBOUND_BY_GLOBAL = {"active_chats", "_agent_cli", "_agent_current", "_agent_last_use", "_agent_reaper", "_cua_grant"}
MODULES = sorted(set(DESTINATION.values()), key=len, reverse=True)


def _sections(lines):
    banners = [(i + 1, re.sub(r"^# -+\s*", "", line).split(":")[0].strip()) for i, line in enumerate(lines) if line.startswith("# ----")]
    starts = [b[0] for b in banners]

    def section(lineno):
        i = bisect.bisect_right(starts, lineno) - 1
        return banners[i][1] if i >= 0 else "(prelude)"
    return section


def _top_level_defs(tree):
    defs = {}
    for node in tree.body:
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                names += [n.id for n in ast.walk(t) if isinstance(n, ast.Name)]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split(".")[0] for a in node.names]
        for n in names:
            defs.setdefault(n, node.lineno)
    return defs


def _chain(node):
    """`studio.services.models.downloads[k].x` -> ["studio", "services", "models", "downloads", "[]", "x"]; None if not a Name chain."""
    out = []
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        out.append(node.attr if isinstance(node, ast.Attribute) else "[]")
        node = node.value
    return [node.id] + out[::-1] if isinstance(node, ast.Name) else None


def _app_attr(node):
    """`app.X` or `studio.<module>.X` -> ("X", "rebind"); `app.X.y` / `app.X[k]` / `studio.<module>.X.y` -> ("X", "mutate")."""
    chain = _chain(node)
    if not chain:
        return None, None
    if chain[0] == "app" and len(chain) > 1:
        rest = chain[1:]
    elif chain[0] == "studio":
        dotted = ".".join(chain)
        mod = next((m for m in MODULES if dotted.startswith(m + ".")), None)
        if mod is None:
            return None, None
        rest = chain[len(mod.split(".")):]
    else:
        return None, None
    if not rest or rest[0] == "[]":
        return None, None
    return rest[0], "rebind" if len(rest) == 1 else "mutate"


def _test_hits(root):
    hits = collections.defaultdict(set)
    for f in sorted((root / "macos").glob("check_*.py")):
        tree = ast.parse(f.read_text())
        string_tuples = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple) and node.value.elts and \
                    all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.value.elts):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        string_tuples[t.id] = [e.value for e in node.value.elts]
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
            for t in targets:
                for el in (t.elts if isinstance(t, ast.Tuple) else [t]):
                    name, kind = _app_attr(el)
                    if name:
                        hits[name].add((f.name, kind))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATORS:
                name, _ = _app_attr(node.func.value)
                if name:
                    hits[name].add((f.name, "mutate"))
        # `self.saved = {k: getattr(app, k) for k in HOOKS}` ... `setattr(app, k, v)`: the names in HOOKS are rebound
        if "HOOKS" in string_tuples:
            for n in string_tuples["HOOKS"]:
                hits[n].add((f.name, "rebind"))
    return hits


def inventory(root=ROOT):
    hits = _test_hits(root)
    names = {n for n in hits if n not in MODULE_OBJECTS}
    points = []
    for n in sorted(names):
        kinds = sorted({k for _, k in hits[n]})
        points.append({
            "name": n,
            "destination": DESTINATION.get(n),
            "patched": "rebind" if "rebind" in kinds else "mutate",
            "suites": sorted({f for f, _ in hits[n]}),
        })
    return {
        "points": points,
        "module_objects_patched": sorted(n for n in hits if n in MODULE_OBJECTS),
    }


def analysis(root=ROOT):
    """The P0 view of the same points: app.py banner, readers in other banners, and `global` rebinds (changes as code moves)."""
    src = (root / "app.py").read_text()
    tree = ast.parse(src)
    section = _sections(src.splitlines())
    defs = _top_level_defs(tree)
    names = {p["name"] for p in inventory(root)["points"]}
    readers = collections.defaultdict(set)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names and node.id in defs:
            s = section(node.lineno)
            if s != section(defs[node.id]):
                readers[node.id].add(s)
    return {n: {"banner": section(defs[n]) if n in defs else None, "read_in_other_sections": sorted(readers[n])} for n in sorted(names)}


def _own_definitions(tree):
    """Top-level names a module defines itself (not imports)."""
    defs = _top_level_defs(tree)
    imported = {(a.asname or a.name).split(".")[0] for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom)) for a in node.names}
    return {n for n in defs if n not in imported}


def import_rule_violations(root=ROOT):
    """P1 import rule: a name rebound at run time (by a suite or by `global`) is defined in its destination module (or still
    in app.py), and no module copies it with `from <owner> import <name>`; readers go through `<owner>.<name>`."""
    rebound = {p["name"] for p in inventory(root)["points"] if p["patched"] == "rebind"} | REBOUND_BY_GLOBAL
    files = {"app": root / "app.py"}
    for path in sorted((root / "studio").glob("**/*.py")):
        files[".".join(path.relative_to(root).with_suffix("").parts)] = path
    trees = {m: ast.parse(p.read_text()) for m, p in files.items()}
    defined = {m: _own_definitions(t) for m, t in trees.items()}
    problems = []
    for name in sorted(rebound):
        where = sorted(m for m, d in defined.items() if name in d)
        dest = DESTINATION.get(name)
        if not dest:
            problems.append(f"{name}: no destination module in macos/inventory_patches.py")
        elif any(m not in ("app", dest) for m in where) or not where:
            problems.append(f"{name}: defined in {where or 'nowhere'}, expected {dest} (or app.py until it moves)")
    for m, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("studio"):
                for a in node.names:
                    if a.name in rebound:
                        problems.append(f"{m}: `from {node.module} import {a.name}` copies a name rebound at run time; use {node.module}.{a.name}")
    return problems


if __name__ == "__main__":
    inv = inventory()
    text = json.dumps(inv, indent=1) + "\n"
    if "--analysis" in sys.argv[1:]:
        text = json.dumps(analysis(), indent=1) + "\n"
    if "--write" in sys.argv[1:]:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text)
        print(f"wrote {FIXTURE.relative_to(ROOT)}: {len(inv['points'])} patch points")
    else:
        sys.stdout.write(text)
