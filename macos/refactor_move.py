"""P1 split tool (temporary: deleted in the P1.main step, see docs/refactor-progress.json).

Generates the studio/ package, the remaining app.py and the retargeted check suites from the P0 versions of those files
(git rev BASE), moving the first N modules of PLAN. Moving is verbatim except for these mechanical rewrites, all listed
in each commit message:

  - a module reads another module's name X that is rebound at run time (by a check suite, `app.X = fake`, or by `global X`)
    as `<owner>.X`, never through `from <owner> import X` (a copy that a later rebind would not reach)
  - `global X` for an X owned by another module becomes assignments to `<owner>.X`
  - `@app.get(...)` route decorators become `@router.get(...)` on the module's APIRouter
  - studio/config.py: ROOT is the parent of studio/, and APP_VERSION is read from app.py (build.sh reads it there)

    python3 -I macos/refactor_move.py graph [N]     # dependency graph of PLAN[:N] (default all) and its cycles
    python3 -I macos/refactor_move.py apply N       # write studio/, app.py and the check suites for PLAN[:N]
"""
import ast
import collections
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "cd64875"                     # P0 tip: app.py before any move
SUITES_BASE = "c34ab16"              # the check suites before any retarget

# Names rebound at run time: by the check suites (macos/fixtures/patch_points.json, patched == rebind) or with `global`.
REBOUND = {
    "AGENT_PROFILE", "AGENT_RUNS", "AGENT_SEARCH_SECONDS", "AGENT_SOCK", "AGENT_SOCK_WAIT_S", "CONTEXT", "CUA_DRIVER",
    "McpClient", "SEARCH_URL", "_agent_adapter", "_agent_cli", "_agent_client", "_agent_daemon_running",
    "_agent_engine_current", "_agent_last_use", "_agent_model", "_agent_now", "_agent_profile_in_use", "_agent_reaper_start",
    "_agent_sleep", "_chat_read_tool", "_download", "_enable_search", "_engine_ready", "_fetch_public_web", "_search_process",
    "_search_proxy", "_search_running", "_start_search", "app_view", "browser_view", "cua_status", "engine_state",
    "ensure_engine", "gate", "model_installed", "start_agent_daemon", "stop_agent_daemon",
    # rebound with `global` in app code
    "active_chats", "_agent_current", "_agent_reaper", "_cua_grant",
}


def base_text(path, rev=BASE):
    return subprocess.run(["git", "-C", str(REPO), "show", f"{rev}:{path}"], capture_output=True, text=True, check=True).stdout


# ---------------------------------------------------------------- scope resolution
class _Frame:
    def __init__(self, kind, names):
        self.kind, self.names = kind, names


def _target_names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _function_locals(fn):
    """Names local to a function or lambda: its arguments and everything it binds, unless declared global."""
    a = fn.args
    names = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    names |= {x.arg for x in (a.vararg, a.kwarg) if x}
    if isinstance(fn, ast.Lambda):
        return names, set()
    glob = set()
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Global):
            glob |= set(n.names)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
            for d in n.decorator_list:
                stack.append(d)
            if not isinstance(n, ast.ClassDef):
                stack.extend(n.args.defaults + [d for d in n.args.kw_defaults if d])
            continue                                 # their bodies are their own scopes
        elif isinstance(n, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names |= {(x.asname or x.name).split(".")[0] for x in n.names}
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.NamedExpr):
            names.add(n.target.id)
        elif isinstance(n, ast.MatchAs) and n.name:
            names.add(n.name)
        stack.extend(ast.iter_child_nodes(n))
    return names - glob, glob


def global_refs(stmt):
    """Every Name node in a top-level statement that refers to a module global: [(node, is_store)], plus `global` stmts."""
    out, globals_ = [], []

    def lookup(name, frames):
        for i, f in enumerate(reversed(frames)):
            if f.kind == "class" and i > 0:
                continue                                     # a class body is not visible to its methods
            if name in f.names:
                return False
        return True

    def visit(node, frames):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                visit(d, frames)
            for d in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                visit(d, frames)
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs + [x for x in (node.args.vararg, node.args.kwarg) if x]:
                if arg.annotation:
                    visit(arg.annotation, frames)
            if node.returns:
                visit(node.returns, frames)
            loc, glob = _function_locals(node)
            for b in node.body:
                visit(b, frames + [_Frame("func", loc)])
            return
        if isinstance(node, ast.Lambda):
            for d in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                visit(d, frames)
            loc, _ = _function_locals(node)
            visit(node.body, frames + [_Frame("func", loc)])
            return
        if isinstance(node, ast.ClassDef):
            for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                visit(d, frames)
            names = set()
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(b.name)
                elif isinstance(b, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    for t in (b.targets if isinstance(b, ast.Assign) else [b.target]):
                        names |= _target_names(t)
            for b in node.body:
                visit(b, frames + [_Frame("class", names)])
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            visit(node.generators[0].iter, frames)               # the first iterable runs in the enclosing scope
            names = set()
            for g in node.generators:
                names |= _target_names(g.target)
            inner = frames + [_Frame("comp", names)]
            for i, g in enumerate(node.generators):
                if i:
                    visit(g.iter, inner)
                for c in g.ifs:
                    visit(c, inner)
            for e in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
                visit(e, inner)
            return
        if isinstance(node, ast.Global):
            globals_.append(node)
            return
        if isinstance(node, ast.Name):
            if not frames or lookup(node.id, frames):
                out.append((node, isinstance(node.ctx, (ast.Store, ast.Del))))
            return
        for child in ast.iter_child_nodes(node):
            visit(child, frames)

    visit(stmt, [])
    return out, globals_


# ---------------------------------------------------------------- the original module
class Stmt:
    def __init__(self, node, lines):
        self.node = node
        first = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        start = first
        while start > 1 and lines[start - 2].startswith("#") and not lines[start - 2].startswith("# --"):
            start -= 1                                   # the comment block directly above belongs to the definition
        self.first, self.start, self.end = first, start, node.end_lineno
        self.text = "\n".join(lines[start - 1:self.end])
        self.defines = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.defines = {node.name}
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                self.defines |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        elif isinstance(node, ast.For):
            self.defines = _target_names(node.target)
        self.key = next(iter(sorted(self.defines))) if len(self.defines) == 1 else f"L{node.lineno}"
        self.refs, self.globals = global_refs(node)
        self.is_route = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name)
                            and d.func.value.id == "app" for d in getattr(node, "decorator_list", []))


class Source:
    def __init__(self, text):
        self.text = text
        self.lines = text.split("\n")
        self.tree = ast.parse(text)
        self.imports = {}                                # alias -> (module or None, name)
        self.stmts = []
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.imports[a.asname or a.name] = (None, a.name)
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    self.imports[a.asname or a.name] = (node.module, a.name)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node is self.tree.body[0]:
                continue                                 # the module docstring
            else:
                self.stmts.append(Stmt(node, self.lines))
        self.owner_stmt = {}
        for s in self.stmts:
            for n in s.defines:
                self.owner_stmt.setdefault(n, s)
        # Section banners and intro comments that stand apart (blank lines) above a statement: [(top, bottom, stmt index)]
        self.blocks = []
        for i, s in enumerate(self.stmts):
            start = s.start
            while True:
                gap = start - 1
                while gap > 1 and not self.lines[gap - 1].strip() and start - gap <= 2:
                    gap -= 1
                if gap >= 1 and self.lines[gap - 1].startswith("#") and (gap < start - 1 or self.lines[gap - 1].startswith("# --")):
                    top = gap
                    while top > 1 and self.lines[top - 2].startswith("#"):
                        top -= 1
                    if top == 1 or not self.lines[top - 2].strip():
                        self.blocks.append((top, gap, i))
                        start = top
                        continue
                break
        self.blocks.sort()

    def float_targets(self, module_of):
        """Each detached comment block goes with the first statement (at or after the one below it) that starts a run of three
        statements planned for one module: a section banner belongs to the section, not to a stray first definition."""
        out = {}
        for top, bottom, i in self.blocks:
            j = i
            while j + 2 < len(self.stmts) and not (module_of(self.stmts[j]) == module_of(self.stmts[j + 1]) == module_of(self.stmts[j + 2])):
                j += 1
            out[(top, bottom)] = self.stmts[j]
        return out


# ---------------------------------------------------------------- the plan
# (module, selectors) in dependency order. A selector is "L<a>-<b>" (statements starting in BASE app.py lines a..b) or a
# top-level name. "-name" removes a statement a range picked up.
PLAN = [
    ("studio.config", ["L52-101", "ENGINES", "-app"]),
    ("studio.infra.sse", ["_sse"]),
    ("studio.infra.system", ["L142-183", "_chat_datetime"]),
    ("studio.infra.text", ["est_tokens", "_clip"]),
    ("studio.infra.edits", ["_post_edit_diagnostics", "_make_diff_preview", "_LINE_NO_RE", "_strip_line_numbers", "_match_edit_block"]),
    ("studio.infra.harness_args", ["_parse_harness_args", "_normalize_harness_args", "_call_args"]),
    ("studio.infra.workspace", ["_chat_workspace", "_chat_turn_id", "_harness_root", "_in_workspace"]),
    ("studio.infra.processes", ["_bg_processes", "_bg_lock", "_run_streamed_subprocess", "_start_bg_shell", "_manage_bg_process"]),
    ("studio.infra.mcp_client", ["AGENT_CUA_TOOLS", "McpError", "AppUnresolved", "McpClient"]),
    ("studio.security.fences", ["L857-861", "_SHELL_BLOCKED_RE", "_harness_path_fence", "_gate_shell_command", "_SAFE_READONLY_BINS",
                                "_SAFE_GIT_SUBCMDS", "_is_safe_readonly_shell"]),
    ("studio.storage.settings", ["load_settings", "save_settings"]),
    ("studio.storage.memory", ["_memory_lock", "_load_agent_memory", "_save_agent_memory"]),
    ("studio.storage.backups", ["_backup_file", "_restore_backup_snap"]),
    ("studio.storage.chats", ["_ID", "_chat_path"]),
    ("studio.security.local_only", ["L106-134"]),
    ("studio.services.models", ["L205-226", "L236-363", "_find_any", "_delete_repo", "-api_system"]),
    ("studio.services.engines", ["L547-679"]),
    ("studio.api.models", ["models", "download_model", "cancel_download", "delete_model", "set_active", "hf_search", "hf_files", "add_custom"]),
    ("studio.api.engines", ["engine_start", "engine_stop", "llm_get"]),
    ("studio.services.attachments", ["L3686-3850", "-upload_attachment", "-attachment_file"]),
    ("studio.api.attachments", ["upload_attachment", "attachment_file"]),
    ("studio.services.images", ["L3899-3958"]),
    ("studio.api.images", ["L3961-4058"]),
    ("studio.services.search", ["L4065-4158", "_reset_index"]),
    ("studio.api.search", ["L4161-4315", "-_reset_index"]),
    ("studio.services.terminal", ["L1667-1794"]),
    ("studio.api.terminal", ["L1797-1871"]),
    ("studio.security.agent_gate", ["_SENSITIVE_AC", "_sensitive_ac", "L5051-5530"]),
    ("studio.services.web", ["L2040-2140"]),
    ("studio.services.agent.cua", ["L4320-4651", "-AGENT_CUA_TOOLS", "-McpError", "-AppUnresolved", "-McpClient"]),
    ("studio.services.agent.views", ["L4656-5044", "-est_tokens", "-_clip", "-_SENSITIVE_AC", "-_sensitive_ac"]),
    ("studio.services.mcp_custom", ["L2258-2366", "-_gate_custom_mcp_tool"]),
    ("studio.security.harness_gate", ["_chat_session_perms", "_gate_custom_mcp_tool", "harness_gate"]),
    ("studio.services.agent.loop", ["L5535-7238", "-_call_args"]),
    ("studio.api.agent", ["L7241-7529"]),
    ("studio.services.harness", ["L760-852", "FILES_PROMPT", "active_chats", "_counted", "_WORKSPACE_INSTRUCTION_FILES",
                                 "_chat_sessions", "_load_workspace_instructions", "_ChatSession",
                                 
                                 "_chat_action_tool", "_read_local_path", "L2143-2253", "L2458-2926",
                                 "_chat_fit_context", "_chat_text", "SLASH_COMMANDS", "_MENTION_RE", "_expand_workspace_mentions",
                                 "_expand_slash_command", "_with_agent_note",
                                 "-api_get_mcp_servers", "-api_save_mcp_server", "-api_delete_mcp_server", "-api_test_mcp_server"]),
    ("studio.api.memory", ["api_get_memory", "api_set_memory", "api_delete_memory"]),
    ("studio.api.undo", ["agent_undo", "agent_undo_turn"]),
    ("studio.api.processes", ["api_list_processes", "api_stop_process"]),
    ("studio.api.mcp", ["api_get_mcp_servers", "api_save_mcp_server", "api_delete_mcp_server", "api_test_mcp_server"]),
    ("studio.api.workspace", ["L3030-3221", "-api_get_slash_commands"]),
    ("studio.api.chat", ["chat_session_reply", "api_compact_chat", "api_get_slash_commands", "_chat_computer_run", "chat"]),
    ("studio.api.chats", ["L3863-3895"]),
    ("studio.api.system", ["api_system", "api_busy", "index"]),
    ("studio.services.updates", ["update_job", "_versions", "_run_update"]),
    ("studio.api.updates", ["versions", "update_engines"]),
    ("studio.api.storage", ["storage", "clear_storage", "uninstall"]),
]

# Commit steps: (name, number of PLAN modules moved). Each step is one commit with every check green.
STEPS = [("helpers-config", 15), ("models-engines", 19), ("attachments", 21), ("images", 23), ("search", 25), ("terminal", 27),
         ("agent", 35), ("harness", 44), ("versions-storage", 47)]


def resolve_plan(src, plan):
    chosen, owner = {}, {}
    by_line = {s.node.lineno: s for s in src.stmts}
    for module, selectors in plan:
        picked = []
        for sel in selectors:
            if sel.startswith("-"):
                s = src.owner_stmt[sel[1:]]
                picked = [p for p in picked if p is not s]
            elif re.fullmatch(r"L\d+-\d+", sel):
                a, b = map(int, sel[1:].split("-"))
                picked += [s for s in src.stmts if a <= s.first <= b and s not in picked]
            else:
                s = src.owner_stmt.get(sel) or by_line.get(int(sel[1:]) if sel[1:].isdigit() else -1)
                if s is None:
                    raise SystemExit(f"{module}: no top-level statement {sel!r}")
                if s not in picked:
                    picked.append(s)
        for s in picked:
            if id(s) in chosen:
                raise SystemExit(f"{s.key} is planned for both {chosen[id(s)]} and {module}")
            chosen[id(s)] = module
            for n in s.defines:
                owner[n] = module
        picked.sort(key=lambda s: s.first)
        yield module, picked


def graph(n=None):
    src = Source(base_text("app.py"))
    plan = list(resolve_plan(src, PLAN[:n]))
    owner = {name: m for m, stmts in plan for s in stmts for name in s.defines}
    edges = collections.defaultdict(lambda: collections.defaultdict(set))
    for module, stmts in plan + [("app", [s for s in src.stmts if not any(s in st for _, st in plan)])]:
        for s in stmts:
            for node, _ in s.refs:
                o = owner.get(node.id, "app" if node.id in src.owner_stmt else None)
                if o and o != module and node.id != "app":
                    edges[module][o].add(node.id)
    order = [m for m, _ in plan] + ["app"]
    for m in order:
        for o, names in sorted(edges[m].items()):
            bad = " <-- BACKWARD" if o != "app" and order.index(o) > order.index(m) or (o == "app" and m != "app") else ""
            print(f"{m:34s} -> {o:34s} {len(names):3d}  {', '.join(sorted(names))[:150]}{bad}")
    return edges


# ---------------------------------------------------------------- generating the files
DOCS = {
    "studio.config": "Paths, ports, the engine table and other constants (ROOT holds code, STATE holds user data).",
    "studio.infra.sse": "Server-sent event framing.",
    "studio.infra.system": "Mac system facts: memory, sizes, pids, the clock tool.",
    "studio.infra.text": "Small text helpers shared by the agent views and the gate.",
    "studio.infra.edits": "Edit matching, diff previews and post-edit syntax diagnostics for the harness.",
    "studio.infra.harness_args": "Repair and normalization of tool-call arguments written by local models.",
    "studio.infra.workspace": "The chat workspace: a ContextVar, never os.chdir.",
    "studio.infra.processes": "Streamed and background subprocesses for run_shell / run_python.",
    "studio.infra.mcp_client": "The stdio MCP JSON-RPC client (CuaDriver and custom MCP servers).",
    "studio.security.fences": "Path and shell-command fences of the agent harness.",
    "studio.security.local_only": "LocalOnly: every route and static file answers only this Mac's own page and local scripts.",
    "studio.security.agent_gate": "The computer-use approval gate: safe / risky / blocked from facts about the target.",
    "studio.security.harness_gate": "The chat harness approval gate for action tools.",
    "studio.storage.settings": "data/settings.json.",
    "studio.storage.memory": "The agent's cross-chat memory file.",
    "studio.storage.backups": "Pre-edit backups behind Undo and Revert turn.",
    "studio.storage.chats": "Saved conversation paths.",
    "studio.services.models": "Models on disk, the catalog and downloads.",
    "studio.services.engines": "The chat engines: one at a time.",
    "studio.services.attachments": "Attachments: images and documents processed once on upload.",
    "studio.services.images": "Image generation jobs (mflux).",
    "studio.services.search": "The file-search service: launch, stop and proxy.",
    "studio.services.terminal": "The Code view's terminal: a login zsh on a pseudo-terminal.",
    "studio.services.web": "Public web search and page reads (public-IP pinned).",
    "studio.services.agent.cua": "The CuaDriver daemon, its permissions and the shared MCP client.",
    "studio.services.agent.views": "Browser and Mac app views the agent model reads.",
    "studio.services.agent.loop": "The computer-use agent: tools, adapter, runs and the step loop.",
    "studio.services.mcp_custom": "Custom MCP servers: clients, tool discovery and calls.",
    "studio.services.harness": "The chat agent harness: tools, the multi-round loop, sub-agents and compaction.",
    "studio.services.updates": "App and engine versions, and engine updates.",
}
THIRD_PARTY = {"httpx", "fastapi", "fastapi.responses", "fastapi.staticfiles"}
CONFIG_ROOT_OLD = 'ROOT = Path(__file__).resolve().parent                       # code (inside the .app when bundled)'
CONFIG_ROOT_NEW = ('ROOT = Path(__file__).resolve().parent.parent                # code (inside the .app when bundled): app.py\'s folder\n'
                   '# The version lives in app.py, where macos/build.sh and release.sh read it with sed.\n'
                   'APP_VERSION = re.search(r\'^APP_VERSION = "(.*)"$\', (ROOT / "app.py").read_text(), re.M).group(1)')
SUITES = ["macos/check_app.py", "macos/check_agent.py"]
# Fixes to the suites made during P1 (applied to SUITES_BASE before the retargets), each with its reason.
SUITE_FIXES = [
    # FastAPI 0.146 keeps an included router as one _IncludedRouter entry in app.routes (matched in place, lazily) instead of
    # copying its routes: the contract walks into it, so it still lists every route in the order requests are matched.
    ("macos/check_app.py",
     '''def _routes():
    """In order: Starlette matches the first route whose path fits, so moving routes into routers must keep this order."""
    out = []
    for r in app.app.routes:
        if hasattr(r, "methods") and r.methods is not None:''',
     '''def _flat_routes(routes):
    for r in routes:
        if type(r).__name__ == "_IncludedRouter":                     # FastAPI >= 0.146: an included APIRouter, matched in place
            assert not r.include_context.prefix, "routers are included without a prefix"
            yield from _flat_routes(r.original_router.routes)
        else:
            yield r


def _routes():
    """In order: Starlette matches the first route whose path fits, so moving routes into routers must keep this order."""
    out = []
    for r in _flat_routes(app.app.routes):
        if hasattr(r, "methods") and r.methods is not None:'''),
]


def module_path(module):
    return REPO / (module.replace(".", "/") + ".py")


class Edits:
    """Text edits on the original source, by AST position (columns are UTF-8 byte offsets)."""
    def __init__(self, lines):
        self.lines = lines
        self.subs = collections.defaultdict(list)          # lineno -> [(col, end_col, text)]
        self.drop = set()

    def sub(self, lineno, col, end_col, text):
        self.subs[lineno].append((col, end_col, text))

    def line(self, lineno):
        raw = self.lines[lineno - 1].encode()
        for col, end, text in sorted(self.subs.get(lineno, []), reverse=True):
            raw = raw[:col] + text.encode() + raw[end:]
        return raw.decode()

    def span(self, a, b):
        return "\n".join(self.line(i) for i in range(a, b + 1) if i not in self.drop)


def _alias(module, texts):
    parts = module.split(".")
    for cand in (parts[-1], "_".join(parts[-2:]), "_" + "_".join(parts[-2:])):
        if not any(re.search(rf"\b{re.escape(cand)}\b", t) for t in texts):
            return cand
    raise SystemExit(f"no free alias for {module}")


def _from_line(module, names, width=120):
    line = f"from {module} import {', '.join(names)}"
    if len(line) <= width:
        return line
    out, cur = [f"from {module} import ("], "   "
    for n in names:
        if len(cur) + len(n) + 2 > width:
            out.append(cur.rstrip())
            cur = "   "
        cur += f" {n},"
    out.append(cur.rstrip())
    return "\n".join(out + [")"])


def _import_lines(src, names, router=False):
    plain, froms = [], collections.OrderedDict()
    order = list(src.imports)
    for alias in sorted(names, key=order.index):
        mod, name = src.imports[alias]
        if mod is None:
            plain.append((name, alias))
        else:
            froms.setdefault(mod, []).append(name if name == alias else f"{name} as {alias}")
    if router:
        froms.setdefault("fastapi", []).insert(0, "APIRouter")
        froms.move_to_end("fastapi", last=False) if len(froms) == 1 else None
    std = [f"import {n}" + (f" as {a}" if a != n else "") for n, a in plain if n not in THIRD_PARTY]
    std += [_from_line(m, ns) for m, ns in froms.items() if m not in THIRD_PARTY]
    third = [f"import {n}" for n, a in plain if n in THIRD_PARTY]
    third += [_from_line(m, ns) for m, ns in froms.items() if m in THIRD_PARTY]
    return std, third


def _rewrite(module, stmts, src, owner, edits, texts):
    """Rewrite one module's statements in `edits`; return (stdlib/3rd-party import names, studio from-imports, module aliases)."""
    imports, from_studio, mod_alias = set(), collections.defaultdict(set), {}
    has_route = False
    for s in stmts:
        cross_globals = set()
        for g in s.globals:
            keep = [n for n in g.names if owner.get(n, module) == module]
            moved = [n for n in g.names if owner.get(n, module) != module]
            cross_globals |= set(moved)
            if moved:
                line = src.lines[g.lineno - 1]
                if keep:
                    edits.sub(g.lineno, g.col_offset, g.end_col_offset, "global " + ", ".join(keep))
                else:
                    if line.strip() != ast.get_source_segment(src.text, g):
                        raise SystemExit(f"{module}: cannot drop a `global` that shares its line: {line!r}")
                    edits.drop.add(g.lineno)
        for node, is_store in s.refs:
            name = node.id
            if name == "app" and owner.get("app", "app") != module:
                deco = [d for d in getattr(s.node, "decorator_list", []) if isinstance(d, ast.Call)
                        and isinstance(d.func, ast.Attribute) and d.func.value is node]
                if not deco:
                    raise SystemExit(f"{module}: {s.key} uses the FastAPI app outside a route decorator")
                edits.sub(node.lineno, node.col_offset, node.end_col_offset, "router")
                has_route = True
                continue
            o = owner.get(name)
            if o is None:
                if name in src.imports:
                    imports.add(name)
                elif name in src.owner_stmt and name not in s.defines:
                    if module != "app":
                        raise SystemExit(f"{module}: {s.key} needs {name}, which is still in app.py")
                continue
            if o == module:
                continue
            if module != "app" and o == "app":
                raise SystemExit(f"{module}: {s.key} needs {name}, which is still in app.py")
            if name in REBOUND or name in cross_globals:
                a = mod_alias.setdefault(o, _alias(o, texts))
                edits.sub(node.lineno, node.col_offset, node.end_col_offset, f"{a}.{name}")
            elif is_store:
                raise SystemExit(f"{module}: {s.key} assigns {name}, owned by {o}, without `global`")
            else:
                from_studio[o].add(name)
    return imports, from_studio, mod_alias, has_route


def _studio_import_lines(from_studio, mod_alias):
    out = []
    for o, a in sorted(mod_alias.items()):
        parent, last = o.rsplit(".", 1)
        out.append(f"from {parent} import {last}" + (f" as {a}" if a != last else ""))
    for o, names in sorted(from_studio.items()):
        out.append(_from_line(o, sorted(names, key=str.lower)))
    return out


def _body(stmts, src, edits, lead):
    block_lines = {i for a, b, _ in src.blocks for i in range(a, b + 1)}
    chunks, prev = [], None
    for s in stmts:
        blocks = lead.get(id(s), [])
        if prev is not None:
            i = src.stmts.index(prev)
            if i + 1 < len(src.stmts) and src.stmts[i + 1] is s and not blocks:
                gap = [k for k in range(prev.end + 1, s.start) if k not in block_lines]
                blanks = min(2, sum(1 for k in gap if not src.lines[k - 1].strip()))
                chunks.append("\n" * (blanks + 1))
            else:
                chunks.append("\n\n\n")
        for a, b in blocks:
            chunks.append("\n".join(src.lines[a - 1:b]) + "\n\n")
        chunks.append(edits.span(s.start, s.end))
        prev = s
    return "".join(chunks) + "\n"


def render_module(module, stmts, src, owner, lead):
    edits = Edits(src.lines)
    texts = [s.text for s in stmts]
    imports, from_studio, mod_alias, has_route = _rewrite(module, stmts, src, owner, edits, texts)
    if module == "studio.config":
        imports.add("re")
    std, third = _import_lines(src, imports, router=has_route)
    doc = DOCS.get(module, f"Routes: {module.rsplit('.', 1)[-1]}.")
    head = [f'"""{doc}', "", "Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).", '"""'] + ([""] + std if std else []) + ([""] + third if third else [])
    studio_lines = _studio_import_lines(from_studio, mod_alias)
    head += ([""] + studio_lines if studio_lines else [])
    if has_route:
        head += ["", "router = APIRouter()"]
    body = _body(stmts, src, edits, lead)
    if module == "studio.config":
        assert CONFIG_ROOT_OLD in body
        body = body.replace(CONFIG_ROOT_OLD, CONFIG_ROOT_NEW)
    return "\n".join(head) + "\n\n\n" + body, has_route


def render_app(rest, plan, src, owner, routers, moved_blocks):
    edits = Edits(src.lines)
    texts = [src.text]
    imports, from_studio, mod_alias, _ = _rewrite("app", rest, src, owner, edits, texts)
    moved_lines = set()
    for _, stmts in plan:
        for s in stmts:
            moved_lines |= set(range(s.start, s.end + 1))
    for a, b in moved_blocks:
        moved_lines |= set(range(a, b + 1))
    first_stmt = src.stmts[0].start
    header = src.lines[:first_stmt - 1]                         # docstring + the original imports, unchanged
    while header and not header[-1].strip():
        header.pop()
    modules = [m for m, _ in plan]
    route_aliases = {}
    for m in routers:
        route_aliases[m] = mod_alias.get(m) or _alias(m, texts + [m.split(".")[-1] for m in mod_alias])
    for m, a in route_aliases.items():
        mod_alias.setdefault(m, a)
    studio_lines = _studio_import_lines(from_studio, mod_alias)
    forward = [
        "",
        "# ---- studio/ split (refactor P1, docs/refactor-progress.json): code moved out of this file module by module.",
        "_MOVED = (" + "".join(f'\n    "{m}",' for m in modules) + "\n)                                                               # dependency order: owners first",
        "for _m in _MOVED:",
        "    importlib.import_module(_m)",
        "",
        "",
        "def _owner_module(name):",
        "    \"\"\"The studio module that defines `name` (the first in dependency order that has it).\"\"\"",
        "    for m in _MOVED:",
        "        mod = sys.modules[m]",
        "        if name in vars(mod):",
        "            return mod",
        "    return None",
        "",
        "",
        "def __getattr__(name):",
        "    \"\"\"The check suites read moved names as app.X: answer live from the owning module, so a rebind there is seen.\"\"\"",
        "    mod = _owner_module(name)",
        "    if mod is None:",
        "        raise AttributeError(f\"module 'app' has no attribute {name!r}\")",
        "    return vars(mod)[name]",
    ]
    out = header + ["import importlib"] + ([""] + studio_lines if studio_lines else []) + forward
    remaining = []
    i = first_stmt
    n = len(src.lines)
    while i <= n:
        if i in moved_lines or i in edits.drop:
            i += 1
            continue
        remaining.append(edits.line(i))
        i += 1
    text = "\n".join(remaining)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip("\n")
    tail = []
    if routers:
        tail = ["", "", "for _router in (" + ", ".join(f"{route_aliases[m]}.router" for m in routers) + "):",
                "    app.include_router(_router)"]
    return "\n".join(out) + "\n\n\n" + text + "\n" + "\n".join(tail) + ("\n" if tail else "")


def retarget_suite(text, owner):
    """`app.X = ...` for a moved X becomes `studio.<owner>.X = ...` in that statement; setattr loops ask patch_guard."""
    tree = ast.parse(text)
    lines = text.split("\n")
    edits = Edits(lines)
    used = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AugAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        flat = [e for t in targets for e in (t.elts if isinstance(t, ast.Tuple) else [t])]
        patched = [t.attr for t in flat if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                   and t.value.id == "app" and owner.get(t.attr, "app") != "app"]
        if not patched:
            continue
        for a in ast.walk(node):
            if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == "app" \
                    and owner.get(a.attr, "app") != "app":
                o = owner[a.attr]
                used.add(o)
                edits.sub(a.value.lineno, a.value.col_offset, a.value.end_col_offset, o)
    out = "\n".join(edits.line(i) for i in range(1, len(lines) + 1))
    for old, new in (("setattr(app, k, v)", "setattr(patch_guard.owner(app, k), k, v)"),
                     ("setattr(app, key, value)", "setattr(patch_guard.owner(app, key), key, value)")):
        out = out.replace(old, new)
    if owner.get("_search_running", "app") != "app":
        old = '"app._search_running = lambda: False; app._stop_search(); app._search_process(); "'
        new = f'"import {owner["_search_running"]}; {owner["_search_running"]}._search_running = lambda: False; app._stop_search(); app._search_process(); "'
        assert old in out, "the search-port subprocess check moved"
        out = out.replace(old, new)
        used.add(owner["_search_running"])
    if used:
        anchor = "import patch_guard  # noqa: E402\n"
        assert anchor in out
        out = out.replace(anchor, anchor + "".join(f"import {m}  # noqa: E402\n" for m in sorted(used)), 1)
    return out


def apply(n):
    src = Source(base_text("app.py"))
    plan = list(resolve_plan(src, PLAN[:n]))
    owner = {name: m for m, stmts in plan for s in stmts for name in s.defines}
    if "studio.config" in [m for m, _ in plan]:
        owner["APP_VERSION"] = "studio.config"
    moved = {id(s) for _, stmts in plan for s in stmts}
    rest = [s for s in src.stmts if id(s) not in moved]
    # APP_VERSION stays in app.py as the literal build.sh reads; config parses it, so code elsewhere reads config's copy.
    rest_owner = dict(owner)
    rest_owner.pop("APP_VERSION", None)
    full = {id(s): m for m, stmts in resolve_plan(src, PLAN) for s in stmts}
    targets = src.float_targets(lambda st: full.get(id(st), "app"))
    lead, moved_blocks = collections.defaultdict(list), []
    for (a, b), st in sorted(targets.items()):
        if id(st) in moved:
            lead[id(st)].append((a, b))
            moved_blocks.append((a, b))
    files, routers = {}, []
    for module, stmts in plan:
        text, has_route = render_module(module, stmts, src, owner, lead)
        files[module_path(module)] = text
        if has_route:
            routers.append(module)
    files[REPO / "app.py"] = render_app(rest, plan, src, rest_owner, routers, moved_blocks)
    for module, _ in plan:
        parts = module.split(".")
        for i in range(1, len(parts)):
            init = REPO.joinpath(*parts[:i], "__init__.py")
            files.setdefault(init, '"""Local Studio backend package (see CLAUDE.md, Architecture)."""\n' if i == 1 else "")
    for suite in SUITES:
        text = base_text(suite, SUITES_BASE)
        for path, a, b in SUITE_FIXES:
            if path == suite:
                assert text.count(a) == 1, (path, a[:60])
                text = text.replace(a, b)
        files[REPO / suite] = retarget_suite(text, owner)
    for path in sorted((REPO / "studio").glob("**/*.py")) if (REPO / "studio").exists() else []:
        if path not in files:
            path.unlink()
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print(f"moved {sum(len(st) for _, st in plan)} statements into {len(plan)} modules; {len(rest)} left in app.py")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "graph"
    if cmd == "graph":
        graph(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif cmd == "apply":
        apply(int(sys.argv[2]))
