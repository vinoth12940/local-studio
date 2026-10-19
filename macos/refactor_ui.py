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
        for r in self.refs:
            r.decl = r.scope.lookup(r.name)

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
PLAN = []
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


def rel(frm, to):
    import os
    r = os.path.relpath(to, os.path.dirname(frm))
    return r if r.startswith(".") else "./" + r


def generate(n):
    html = base_text("index.html", BASE)
    an, off, units = units_of(html)
    plan = resolve_plan(an, off, units, PLAN[:n])
    moved = {id(u) for _, us in plan for u in us}
    rest = [u for u in units if id(u) not in moved]
    modules = plan + [("main", rest)]
    owner = {}
    info = {}
    for module, us in modules:
        decl, refs = set(), []
        for u in us:
            d, r = unit_names(an, off, u)
            decl |= d
            refs += r
        info[module] = (decl, refs)
        for name in decl:
            owner[name] = module
    path = lambda m: f"frontend/{m}.mjs"                                          # noqa: E731
    html_lines = html.split("\n")
    exports = collections.defaultdict(set)
    imports = collections.defaultdict(lambda: collections.defaultdict(set))
    problems = []
    for module, (decl, refs) in info.items():
        for r in refs:
            o = owner.get(r.name)
            if o is None or o == module:
                continue
            if r.write:
                problems.append(f"{module}: assigns {r.name} (owned by {o}) at index.html line {off + r.node.start_point[0] + 1}")
            imports[module][o].add(r.name)
            exports[o].add(r.name)
    for name in BRIDGE:
        exports[owner[name]].add(name)
    files = {}
    for module, us in modules:
        head = [f"// {module}.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json)."]
        if module == "main":
            head = ["// The UI's entry point: index.html loads it as a module. What is not split into frontend/ yet still lives here,",
                    "// in its original order (the refactor's P2 split, docs/refactor-progress.json).", 'import "./globals.mjs";']
        for o, names in sorted(imports[module].items()):
            head.append(f"import {{ {', '.join(sorted(names, key=str.lower))} }} from \"{rel(path(module), path(o))}\";")
        body = []
        prev = None
        for u in us:
            if prev is not None and units.index(prev) + 1 == units.index(u):
                body.extend(html_lines[k - 1] for k in range(prev.last + 1, u.last + 1))   # neighbours: copy what lies between
            else:
                if prev is not None:
                    body.append("")
                body.extend(html_lines[k - 1] for k in sorted(set(u.lead) | set(range(u.first, u.last + 1))))
            prev = u
        tail = ["", "export {", "  " + _wrap(sorted(info[module][0], key=str.lower)), "};"] if info[module][0] else []
        files[path(module)] = "\n".join(head + [""] + body + tail) + "\n"
    # globals bridge and console handle
    g = ["// The page's globals, set in one place (refactor P2). Inline handlers and innerHTML templates call these by name, and",
         "// window.LS is a read-only console handle for debugging (CLAUDE.md: render synthetic turns into LS.current).",
         'import * as main from "./main.mjs";']
    mods = [m for m, _ in plan]
    for m in mods:
        g.append(f'import * as {m.replace("/", "_")} from "{rel("frontend/globals.mjs", path(m))}";')
    g.append("")
    for name in BRIDGE:
        g.append(f"window.{name} = (...args) => {owner[name].replace('/', '_') if owner[name] != 'main' else 'main'}.{name}(...args);")
    g.append("window.LS = new Proxy({}, {")
    g.append("  get: (_, k) => [main" + "".join(f", {m.replace('/', '_')}" for m in mods) + "].map((m) => m[k]).find((v) => v !== undefined),")
    g.append("});")
    files["frontend/globals.mjs"] = "\n".join(g) + "\n"
    a, b, _ = index_script(html)
    new_html = html[:a - len("<script>")] + '<script type="module" src="frontend/main.mjs"></script>' + html[b + len("</script>"):]
    files["index.html"] = new_html
    return files, problems


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
    files, problems = generate(n)
    if problems:
        raise SystemExit("cross-module assignments (need a store):\n" + "\n".join(problems))
    for old in (REPO / "frontend").glob("**/*.mjs") if (REPO / "frontend").exists() else []:
        if old.relative_to(REPO).as_posix() not in files:        # every module under frontend/ is generated during P2
            old.unlink()
    for p, text in files.items():
        (REPO / p).parent.mkdir(parents=True, exist_ok=True)
        (REPO / p).write_text(text)
    print(f"wrote {len(files)} files")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "hazards"
    if cmd == "hazards":
        hazards((REPO / "index.html").read_text())
    elif cmd == "apply":
        apply(int(sys.argv[2]))
