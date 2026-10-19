"""P2 split tool for the UI (temporary: deleted when P2 ends, see docs/refactor-progress.json).

Parses index.html's inline script with tree-sitter and resolves every identifier to its scope, so the script can move into
ES modules under frontend/ with only mechanical edits. Run it with the pinned parser (dev-only, never shipped):

    uv run -q --no-project --python 3.12 --with tree-sitter==0.26.0 --with tree-sitter-javascript==0.25.0 \\
        python -I macos/refactor_ui.py hazards        # strict-mode hazards of running the script as a module
"""
import collections
import subprocess
import sys
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_javascript

REPO = Path(__file__).resolve().parent.parent
PARSER = Parser(Language(tree_sitter_javascript.language()))

FUNCTIONS = {"function_declaration", "generator_function_declaration", "function_expression", "function",
             "generator_function", "arrow_function", "method_definition"}
BLOCKS = {"statement_block", "for_statement", "for_in_statement", "switch_body", "catch_clause", "class_body"}


class Scope:
    def __init__(self, kind, parent, node):
        self.kind, self.parent, self.node, self.names = kind, parent, node, {}

    def function_scope(self):
        s = self
        while s.kind not in ("function", "program"):
            s = s.parent
        return s

    def lookup(self, name):
        s = self
        while s is not None:
            if name in s.names:
                return s
            s = s.parent
        return None


class Ref:
    __slots__ = ("node", "name", "scope", "write", "decl")

    def __init__(self, node, name, scope, write):
        self.node, self.name, self.scope, self.write, self.decl = node, name, scope, write, None


class Analysis:
    """Scopes, declarations and resolved references of one script."""

    def __init__(self, code):
        self.code = code
        self.src = code.encode()
        self.tree = PARSER.parse(self.src)
        self.root = self.tree.root_node
        self.program = Scope("program", None, self.root)
        self.scope_of = {}                 # node id -> innermost scope
        self.bindings = set()              # node ids of identifiers that declare a name
        self.refs = []
        self.block_functions = []          # (name, scope) of function declarations inside blocks
        self._declare_pass(self.root, self.program)
        self._ref_pass(self.root, self.program)
        self._refs_by_id = collections.defaultdict(list)
        for r in self.refs:
            r.decl = r.scope.lookup(r.name)
            self._refs_by_id[r.node.id].append(r)

    def text(self, n):
        return self.src[n.start_byte:n.end_byte].decode()

    # -- declarations
    def _pattern_names(self, n, out):
        t = n.type
        if t in ("identifier", "shorthand_property_identifier_pattern"):
            out.append(n)
        elif t in ("object_pattern", "array_pattern"):
            for c in n.named_children:
                self._pattern_names(c, out)
        elif t == "pair_pattern":
            self._pattern_names(n.child_by_field_name("value"), out)
        elif t in ("assignment_pattern", "object_assignment_pattern"):
            self._pattern_names(n.child_by_field_name("left"), out)
        elif t == "rest_pattern":
            for c in n.named_children:
                self._pattern_names(c, out)
        return out

    def _bind(self, scope, nodes):
        for idn in nodes:
            scope.names.setdefault(self.text(idn), idn)
            self.bindings.add(idn.id)

    def _new_scope(self, n, scope):
        if n.type in FUNCTIONS:
            s = Scope("function", scope, n)
            if n.type in ("function_expression", "function", "generator_function"):
                name = n.child_by_field_name("name")
                if name is not None:
                    self._bind(s, [name])                      # a named function expression sees its own name
            params = n.child_by_field_name("parameters") or n.child_by_field_name("parameter")
            if params is not None:
                if params.type == "identifier":
                    self._bind(s, [params])
                else:
                    for p in params.named_children:
                        self._bind(s, self._pattern_names(p, []))
            return s
        if n.type in BLOCKS and not (n.type == "statement_block" and n.parent is not None and n.parent.type in FUNCTIONS):
            s = Scope("block", scope, n)
            if n.type == "catch_clause":
                p = n.child_by_field_name("parameter")
                if p is not None:
                    self._bind(s, self._pattern_names(p, []))
            return s
        return None

    def _declare_pass(self, n, scope):
        s = self._new_scope(n, scope)
        if s is not None:
            scope = s
        self.scope_of[n.id] = scope
        t = n.type
        if t in ("function_declaration", "generator_function_declaration"):
            target = n.parent and self.scope_of.get(n.parent.id) or scope.parent
            outer = scope.parent                                   # the scope the declaration sits in
            self._bind(outer, [n.child_by_field_name("name")])
            if outer.kind == "block":
                self.block_functions.append((self.text(n.child_by_field_name("name")), outer))
        elif t == "class_declaration":
            self._bind(scope, [n.child_by_field_name("name")])
        elif t == "import_specifier":
            self._bind(self.program, [n.child_by_field_name("alias") or n.child_by_field_name("name")])
        elif t == "namespace_import":
            self._bind(self.program, [c for c in n.named_children if c.type == "identifier"])
        elif t in ("lexical_declaration", "variable_declaration"):
            target = scope.function_scope() if t == "variable_declaration" else scope
            for d in n.named_children:
                if d.type == "variable_declarator":
                    self._bind(target, self._pattern_names(d.child_by_field_name("name"), []))
        elif t == "for_in_statement" and n.child_by_field_name("kind") is not None:
            kind = self.text(n.child_by_field_name("kind"))                # for (const x of ...), for (let [a, b] of ...)
            target = scope.function_scope() if kind == "var" else scope
            self._bind(target, self._pattern_names(n.child_by_field_name("left"), []))
        for c in n.children:
            self._declare_pass(c, scope)

    # -- references
    def _targets(self, n, out):
        """Identifiers written by an assignment target (identifier or destructuring pattern)."""
        t = n.type
        if t in ("identifier", "shorthand_property_identifier_pattern"):
            out.append(n)
        elif t in ("object_pattern", "array_pattern", "rest_pattern"):
            for c in n.named_children:
                self._targets(c, out)
        elif t == "pair_pattern":
            self._targets(n.child_by_field_name("value"), out)
        elif t in ("assignment_pattern", "object_assignment_pattern"):
            self._targets(n.child_by_field_name("left"), out)
        return out

    def _ref_pass(self, n, scope):
        scope = self.scope_of.get(n.id, scope)
        t = n.type
        writes = []
        if t in ("assignment_expression", "augmented_assignment_expression"):
            writes = self._targets(n.child_by_field_name("left"), [])
        elif t == "update_expression":
            arg = n.child_by_field_name("argument")
            if arg is not None and arg.type == "identifier":
                writes = [arg]
        elif t == "for_in_statement" and n.child_by_field_name("kind") is None:
            left = n.child_by_field_name("left")
            if left is not None and left.type in ("identifier", "object_pattern", "array_pattern"):
                writes = self._targets(left, [])
        for w in writes:
            if w.id not in self.bindings:
                self.refs.append(Ref(w, self.text(w), self.scope_of.get(w.id, scope), True))
                self.bindings.add(w.id)                            # not counted again as a read
        if t == "export_statement" and n.child_by_field_name("source") is not None:
            return                                                     # export { a } from "./x.mjs": names of that module
        if t in ("identifier", "shorthand_property_identifier") and n.id not in self.bindings:
            self.refs.append(Ref(n, self.text(n), scope, False))
        for c in n.children:
            self._ref_pass(c, scope)


def index_script(html):
    a = html.rindex("<script>") + len("<script>")
    b = html.rindex("</script>")
    return a, b, html[a:b]


def base_text(path, rev):
    return subprocess.run(["git", "-C", str(REPO), "show", f"{rev}:{path}"], capture_output=True, text=True, check=True).stdout


def hazards(html):
    a, _, code = index_script(html)
    first_line = html[:a].count("\n") + 1
    an = Analysis(code)
    line = lambda n: first_line + n.start_point[0]                               # noqa: E731
    implicit = collections.defaultdict(list)
    free = collections.Counter()
    for r in an.refs:
        if r.decl is None:
            free[r.name] += 1
            if r.write:
                implicit[r.name].append(line(r.node))
    print("free names (browser globals the script uses):", len(free))
    print("  ", " ".join(sorted(free)))
    print("assignments to undeclared names (throw in strict mode unless the global property exists):")
    for name, lines in sorted(implicit.items()):
        print(f"   {name}: lines {lines[:8]}")
    print("function declarations inside blocks:")
    for name, scope in an.block_functions:
        outside = [line(r.node) for r in an.refs if r.name == name and r.decl is None]
        print(f"   {name} (line {line(scope.node)}): unresolved uses outside its block: {outside[:8]}")
    for word in ("document.currentScript", "arguments.callee", "\"use strict\"", "'use strict'"):
        print(f"{word}: {code.count(word)}")
    return an


# ---------------------------------------------------------------- generating frontend/
BASE = "fad4da6"                      # index.html before P2 (unchanged since P0)
NOT_NAMES = {"await"}                 # tree-sitter reads `await (x)` inside async functions as a call to an identifier
# (module, selectors): a selector is "L<a>-<b>" (units starting in BASE index.html lines a..b) or a top-level name; "-name"
# removes a unit a range picked up. Units not planned stay in main.mjs. Modules are listed in dependency order.
PLAN = [
    ("core/util", ["L1235-1259"]),
    ("core/markdown", ["L1260-1309"]),
    ("ui/theme", ["L1311-1318"]),
    ("ui/views", ["L1320-1369"]),
    ("ui/engine", ["L1370-1427"]),
    ("chat/list", ["L1428-1554", "-thinking"]),
    ("chat/thread", ["L1555-2009"]),
    ("chat/attachments", ["L2011-2054"]),
    ("chat/composer", ["thinking", "L2055-2240"]),
    ("chat/live", ["L2241-2524"]),
    ("chat/computer", ["L2525-2685"]),
    ("images/images", ["L2686-2758"]),
    ("code/editor", ["L2759-2977"]),
    ("code/terminal", ["L2978-3206"]),
    ("library/library", ["L3207-3365"]),
    ("settings/settings", ["L3366-3452"]),
    ("search/search", ["L3453-3644"]),
    ("agent/state", ["L3645-3665"]),
    ("agent/helpers", ["L3666-3745"]),
    ("agent/agent", ["L3746-4092"]),
    ("settings/agent", ["L4093-4180"]),
]
# Load-time statements allowed to run later than before, with the reason (the tdz check still covers reads of their names).
ORDER_OK = {
    1433: "let thinking/effort = store.get(...): moved to chat/composer, which assigns them; reading localStorage later is harmless",
}
BRIDGE = ["showView"]                 # names inline handlers and innerHTML templates call (onclick="showView('library')")


class Unit:
    def __init__(self, nodes, first, last, lines):
        self.nodes, self.first, self.last = nodes, first, last         # 1-based lines in index.html
        self.lead = []                                                 # comment lines that travel with it (line numbers)
        self.text_lines = lines

    @property
    def start(self):
        return min([self.first] + self.lead)


def units_of(html):
    a, _, code = index_script(html)
    off = html[:a].count("\n")                       # script line 0 is index.html line off + 1
    an = Analysis(code)
    html_lines = html.split("\n")
    units, comments = [], []
    for n in an.root.children:
        first, last = off + n.start_point[0] + 1, off + n.end_point[0] + 1
        if n.type == "comment" and not (units and units[-1].last == first):
            comments.append((first, last))
            continue
        if units and units[-1].last == first:                    # shares a line with the previous unit: one unit
            units[-1].nodes.append(n)
            units[-1].last = max(units[-1].last, last)
            continue
        units.append(Unit([n], first, last, html_lines))
    # comment lines directly above a unit go with it; a comment block standing apart (blank line below) goes with the next
    # unit too, which is how the section headers (// ---------------- views) travel
    by_first = {u.first: u for u in units}
    pending = []
    for c in comments:
        pending.append(c)
        lines_ = list(range(c[0], c[1] + 1))
        nxt = next((u for u in units if u.first > c[1]), None)
        if nxt is not None:
            nxt.lead.extend(lines_)
    for u in units:
        u.lead.sort()
    return an, off, units


def unit_names(an, off, u):
    """(declared top-level names, refs to top-level names) of a unit."""
    lo, hi = u.first - off - 1, u.last - off - 1
    declared = {name for name, idn in an.program.names.items() if lo <= idn.start_point[0] <= hi}
    refs = [r for r in an.refs if r.decl is an.program and lo <= r.node.start_point[0] <= hi and r.name not in NOT_NAMES]
    return declared, refs


def resolve_plan(an, off, units, plan):
    owner_unit = {}
    for u in units:
        for name in unit_names(an, off, u)[0]:
            owner_unit[name] = u
    out, taken = [], {}
    for module, selectors in plan:
        picked = []
        for sel in selectors:
            if sel.startswith("-"):
                picked = [u for u in picked if u is not owner_unit[sel[1:]]]
            elif sel.startswith("L") and "-" in sel and sel[1:].replace("-", "").isdigit():
                lo, hi = map(int, sel[1:].split("-"))
                picked += [u for u in units if lo <= u.first <= hi and u not in picked]
            else:
                u = owner_unit.get(sel)
                if u is None:
                    raise SystemExit(f"{module}: no top-level declaration {sel!r}")
                if u not in picked:
                    picked.append(u)
        for u in picked:
            if id(u) in taken:
                raise SystemExit(f"unit at line {u.first} is planned for {taken[id(u)]} and {module}")
            taken[id(u)] = module
        out.append((module, sorted(picked, key=lambda u: u.first)))
    return out


LOAD_SAFE = {"arrow_function", "function_expression", "function", "generator_function", "class_body"}


def _effectful(an, node):
    """Does a top-level statement do something when the module loads (beyond declaring a function or a plain value)?"""
    if node.type in ("function_declaration", "generator_function_declaration", "comment", "empty_statement"):
        return False
    def walk(n):
        if n.type in LOAD_SAFE and not (n.parent is not None and n.parent.type == "parenthesized_expression"
                                          and n.parent.parent is not None and n.parent.parent.type == "call_expression"):
            return False
        if n.type in ("call_expression", "new_expression", "await_expression", "assignment_expression",
                      "augmented_assignment_expression", "update_expression"):
            return True
        return any(walk(c) for c in n.children)
    return walk(node)


def eval_order(modules, imports):
    """ES module evaluation order from main.mjs: depth first over the import statements in the order they are written."""
    order, state = [], {}
    def visit(m):
        if state.get(m):
            return
        state[m] = "visiting"
        for dep in imports.get(m, []):
            visit(dep)
        order.append(m)
        state[m] = "done"
    visit("main")
    return order


def rel(frm, to):
    import os
    r = os.path.relpath(to, os.path.dirname(frm))
    return r if r.startswith(".") else "./" + r


def _store_name(module):
    return module.split("/")[-1] + "State"


def generate(n, report=False):
    html = base_text("index.html", BASE)
    an, off, units = units_of(html)
    plan = resolve_plan(an, off, units, PLAN[:n])
    moved = {id(u) for _, us in plan for u in us}
    modules = plan + [("main", [u for u in units if id(u) not in moved])]
    rank = {m: i for i, (m, _) in enumerate(modules)}
    owner, info = {}, {}
    for module, us in modules:
        decl, refs = set(), []
        for u in us:
            d, r = unit_names(an, off, u)
            decl |= d
            refs += r
        info[module] = (decl, refs)
        for name in decl:
            owner[name] = module
    # A name assigned outside the module that declares it becomes a property of that module's store object (an ES module
    # cannot assign a binding it imports). Every reference to it, in every module, reads and writes the property.
    stored = {}
    for module, (decl, refs) in info.items():
        for r in refs:
            if r.write and owner.get(r.name) not in (None, module):
                stored[r.name] = owner[r.name]
    for m in set(stored.values()):
        if _store_name(m) in an.code:
            raise SystemExit(f"store name {_store_name(m)} is already used in the script")
    # -- edits on the script, by byte offset; each keeps its line count so units keep their lines
    a, b, _ = index_script(html)
    edits = []                                                    # (start, end, text) in script bytes
    for r in an.refs:
        if r.decl is an.program and r.name in stored:
            obj = _store_name(stored[r.name])
            if r.node.type in ("shorthand_property_identifier", "shorthand_property_identifier_pattern"):
                edits.append((r.node.start_byte, r.node.end_byte, f"{r.name}: {obj}.{r.name}"))
            else:
                edits.append((r.node.start_byte, r.node.end_byte, f"{obj}.{r.name}"))
    created = set()
    for node in an.root.children:
        if node.type != "lexical_declaration":
            continue
        decls = [d for d in node.named_children if d.type == "variable_declarator"]
        names = [an.text(d.child_by_field_name("name")) for d in decls]
        if not any(nm in stored for nm in names):
            continue
        kind = an.text(node.children[0])
        keep = [an.text(d) for d, nm in zip(decls, names) if nm not in stored]
        props = [(nm, an.text(d.child_by_field_name("value")) if d.child_by_field_name("value") else "undefined")
                 for d, nm in zip(decls, names) if nm in stored]
        obj = _store_name(stored[props[0][0]])
        out = f"{kind} {', '.join(keep)}; " if keep else ""
        if obj not in created:
            created.add(obj)
            out += "const " + obj + " = { " + ", ".join(f"{k}: {v}" for k, v in props) + " };"
        else:
            out += " ".join(f"{obj}.{k} = {v};" for k, v in props)
        semi = node.end_byte
        edits.append((node.start_byte, semi, out))
    script = an.src
    # drop reference edits that fall inside a rewritten declaration (its initializers are copied as text, already edited below)
    decl_spans = [(s_, e_) for s_, e_, t in edits if t.startswith(("let ", "const ", "var ")) or t.startswith(tuple(_store_name(m) for m in set(stored.values())))]
    def inside(e):
        return any(s_ < e[0] and e[1] <= e_ for s_, e_ in decl_spans if (s_, e_) != (e[0], e[1]))
    final = sorted([e for e in edits if not inside(e)], key=lambda e: e[0], reverse=True)
    # initializers of moved declarators must carry their own reference edits: re-run edits on their text
    out_src = script
    for s_, e_, t in final:
        out_src = out_src[:s_] + t.encode() + out_src[e_:]
    new_html = html[:a] + out_src.decode() + html[b:]
    assert new_html.count("\n") == html.count("\n"), "an edit changed the line count"
    html_lines = new_html.split("\n")
    # -- imports, exports, files
    path = lambda m: f"frontend/{m}.mjs"                                          # noqa: E731
    imports = collections.defaultdict(lambda: collections.defaultdict(set))
    for module, (decl, refs) in info.items():
        for r in refs:
            o = owner.get(r.name)
            if o is None or o == module:
                continue
            imports[module][o].add(_store_name(o) if r.name in stored else r.name)
    for name in BRIDGE:
        if name in stored:
            raise SystemExit(f"bridge name {name} is in a store")
    files, dep_order = {}, {"globals": ["modules"], "modules": [m for m, _ in plan]}
    for m, _ in plan:
        dep_order[m] = ["modules"]
    for module, us in modules:
        head = [f"// {module}.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json)."]
        if module == "main":
            head = ["// The UI's entry point: index.html loads it as a module. What is not split into frontend/ yet still lives here,",
                    "// in its original order (the refactor's P2 split, docs/refactor-progress.json).", 'import "./globals.mjs";']
        names = sorted({x for o in imports[module] for x in imports[module][o]}, key=str.lower)
        if module == "main":
            dep_order["main"] = ["globals", "modules"]
        if names:
            head.append(_import_line(names, rel(path(module), "frontend/modules.mjs")))
        body, prev = [], None
        for u in us:
            if prev is not None and units.index(prev) + 1 == units.index(u):
                body.extend(html_lines[k - 1] for k in range(prev.last + 1, u.last + 1))
            else:
                if prev is not None:
                    body.append("")
                body.extend(html_lines[k - 1] for k in sorted(set(u.lead) | set(range(u.first, u.last + 1))))
            prev = u
        decl = info[module][0]
        exported = sorted((decl - set(stored)) | ({_store_name(module)} if module in stored.values() else set()), key=str.lower)
        tail = ["", "export {", "  " + _wrap(exported), "};"] if exported else []
        files[path(module)] = "\n".join(head + [""] + body + tail) + "\n"
    mods = [m for m, _ in plan]
    hub = ["// Every module imports the names it uses from here, never from another module directly. ES modules run their imports",
           "// first, so imports between modules would change the order the UI's start-up statements run in. This file lists the",
           "// modules in the order their code had in index.html, which is the order they run in (refactor P2).",
           "// Generated by macos/refactor_ui.py."]
    for m in mods:
        decl = info[m][0]
        names = sorted((decl - set(stored)) | ({_store_name(m)} if m in stored.values() else set()), key=str.lower)
        if names:
            hub.append("export {\n  " + _wrap(names) + f"\n}} from \"{rel('frontend/modules.mjs', path(m))}\";")
        else:
            hub.append(f'import "{rel("frontend/modules.mjs", path(m))}";')
    rest_names = sorted(info["main"][0] - set(stored) | ({_store_name("main")} if "main" in stored.values() else set()), key=str.lower)
    if rest_names:                                                 # not split out yet: main.mjs, which already runs last
        hub.append("export {\n  " + _wrap(rest_names) + "\n} from \"./main.mjs\";")
    files["frontend/modules.mjs"] = "\n".join(hub) + "\n"
    alias = lambda m: m.replace("/", "_")                                            # noqa: E731
    g = ["// The page's globals, set in one place (refactor P2). Inline handlers and innerHTML templates call these by name, and",
         "// window.LS is a console handle for debugging (CLAUDE.md: render synthetic turns into LS.current, then LS.renderThread()).",
         'import * as main from "./main.mjs";']
    g += [f'import * as {alias(m)} from "{rel("frontend/globals.mjs", path(m))}";' for m in mods]
    g.insert(3, 'import "./modules.mjs";                                   // the modules run in their original order first')
    g.append("")
    for name in BRIDGE:
        g.append(f"window.{name} = (...args) => {alias(owner[name])}.{name}(...args);")
    spaces = ["main"] + [alias(m) for m in mods]
    stores = sorted({f"{alias(m)}.{_store_name(m)}" for m in set(stored.values())})
    g += ["const SPACES = [" + ", ".join(spaces) + "];",
          "const STORES = () => [" + ", ".join(stores) + "];            // names more than one module assigns live in these",
          "window.LS = new Proxy({}, {",
          "  get: (_, k) => STORES().find((s) => k in s)?.[k] ?? SPACES.map((m) => m[k]).find((v) => v !== undefined),",
          "  set: (_, k, v) => { const s = STORES().find((x) => k in x); if (!s) return false; s[k] = v; return true; },",
          "});"]
    files["frontend/globals.mjs"] = "\n".join(g) + "\n"
    a2, b2, _ = index_script(new_html)
    files["index.html"] = html[:a - len("<script>")] + '<script type="module" src="frontend/main.mjs"></script>' + html[b + len("</script>"):]
    checks = _checks(an, off, units, modules, owner, stored, dep_order, files)
    return files, checks, stored


def _import_line(names, path_):
    line = f"import {{ {', '.join(names)} }} from \"{path_}\";"
    if len(line) <= 120:
        return line
    return "import {\n  " + _wrap(names) + f"\n}} from \"{path_}\";"


def _checks(an, off, units, modules, owner, stored, dep_order, files):
    """Evaluation order and free names of the generated modules; returns a list of problems."""
    problems = []
    unit_module = {id(u): m for m, us in modules for u in us}
    order = eval_order([m for m, _ in modules], dep_order)
    pos = {m: i for i, m in enumerate(order)}
    # side effects at load: same relative order as in the original script
    eff = [u for u in units if any(_effectful(an, nd) for nd in u.nodes)]
    seq = sorted(eff, key=lambda u: (pos[unit_module[id(u)]], u.first))
    moved_early = [(u.first, unit_module[id(u)]) for i, u in enumerate(seq) if any(v.first > u.first for v in seq[:i])]
    for line_, m in moved_early:
        if line_ in ORDER_OK:
            continue
        problems.append(f"order: the load-time statement at index.html line {line_} ({m}) now runs before statements that preceded it")
    # load-time reads: a binding that was initialized before the reading statement in index.html must still be (functions are
    # always initialized; a read that was already in its temporal dead zone before, e.g. inside a try, stays as it was)
    run_at = {}                                                   # unit id -> (module position, original line)
    for m, us in modules:
        for u in us:
            run_at[id(u)] = (pos[m], u.first)
    unit_of_line = {}
    for u in units:
        for k in range(u.first, u.last + 1):
            unit_of_line[k] = u
    for m, us in modules:
        for u in us:
            for nd in u.nodes:
                if not _effectful(an, nd) and nd.type not in ("lexical_declaration", "variable_declaration"):
                    continue
                for r in _load_time_refs(an, nd):
                    d = an.program.names.get(r.name)
                    if d is None or (d.parent is not None and d.parent.type in ("function_declaration", "generator_function_declaration")):
                        continue
                    du = unit_of_line.get(off + d.start_point[0] + 1)
                    if du is None or du is u:
                        continue
                    was_ready = du.first < u.first
                    is_ready = run_at[id(du)] < run_at[id(u)]
                    if was_ready and not is_ready:
                        problems.append(f"tdz: index.html line {u.first} ({m}) reads {r.name} (line {du.first}) before it is initialized now")
    # free names of each generated module are browser globals the original script already used
    baseline = {r.name for r in an.refs if r.decl is None} | NOT_NAMES
    for p, text in files.items():
        if p.endswith(".mjs"):
            g = Analysis(text)
            extra = sorted({r.name for r in g.refs if r.decl is None} - baseline - ({"Proxy"} if p.endswith("globals.mjs") else set()))
            if extra:
                problems.append(f"free: {p} uses names nothing declares or imports: {extra}")
    return problems


def _load_time_refs(an, node):
    """Top-level references a statement makes while the module loads: not inside function bodies, except functions called
    right there (IIFEs) and top-level functions the statement calls (followed once)."""
    out = []
    def walk(n, seen):
        if n.type in LOAD_SAFE and not (n.parent is not None and n.parent.type == "parenthesized_expression"
                                          and n.parent.parent is not None and n.parent.parent.type == "call_expression"):
            return
        for r in an._refs_by_id.get(n.id, []):
            out.append(r)
            if r.name not in seen and n.parent is not None and n.parent.type == "call_expression" and r.decl is an.program:
                d = an.program.names.get(r.name)
                fn = d.parent if d is not None else None
                if fn is not None and fn.type in ("function_declaration",):
                    seen.add(r.name)
                    body = fn.child_by_field_name("body")
                    for c in body.children:
                        walk(c, seen)
        for c in n.children:
            walk(c, seen)
    walk(node, set())
    return [r for r in out if r.decl is an.program]


def _wrap(names, width=118):
    lines, cur = [], ""
    for n in names:
        if len(cur) + len(n) + 2 > width:
            lines.append(cur.rstrip())
            cur = ""
        cur += n + ", "
    lines.append(cur.rstrip().rstrip(","))
    return "\n  ".join(lines)


def apply(n):
    files, problems, stored = generate(n)
    if problems:
        raise SystemExit("not generated:\n" + "\n".join(problems))
    for old in (REPO / "frontend").glob("**/*.mjs") if (REPO / "frontend").exists() else []:
        if old.relative_to(REPO).as_posix() not in files:        # every module under frontend/ is generated during P2
            old.unlink()
    for p, text in files.items():
        (REPO / p).parent.mkdir(parents=True, exist_ok=True)
        (REPO / p).write_text(text)
    print(f"wrote {len(files)} files; stores: {dict(sorted(stored.items()))}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "hazards"
    if cmd == "hazards":
        hazards((REPO / "index.html").read_text())
    elif cmd == "apply":
        apply(int(sys.argv[2]))
    elif cmd == "check":
        files, problems, stored = generate(int(sys.argv[2]) if len(sys.argv) > 2 else len(PLAN))
        print("stores:", dict(sorted(stored.items())))
        print("\n".join(problems) or "no problems")
