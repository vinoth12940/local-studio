"""Browser and Mac app views the agent model reads.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import re
import textwrap

from studio.infra.text import _clip, est_tokens
from studio.security.agent_gate import _sensitive_ac


# -- views: CuaDriver page/app snapshots -> compact text with short refs (e1..eN) for a 32k-context model

_OUTLINE_LINE = re.compile(r'^(\s*)- ([\w-]+)(?: "(.*?)"(?= \[|\s*$))?')
_VIEW_TEXT_ROLES = frozenset(("statictext", "text", "heading"))
_VIEW_PLAIN_ROLES = frozenset((                              # page structure that is neither a control nor text worth a line
    "generic image figure paragraph none rootwebarea group list listitem labeltext legend form main banner navigation "
    "contentinfo search region article section table row cell rowgroup columnheader presentation separator").split())
_VIEW_LANDMARKS = frozenset(("main", "banner", "navigation", "contentinfo", "search", "region"))
_VIEW_FLAGS = ("required", "focused", "selected", "expanded", "pressed", "disabled", "invalid")
# A button counts as a submit when the DOM says type=submit, or (no DOM info) its label starts like one.
# (and a button inside a <form> with no DOM type is one too: HTML's default button type is submit)
_SUBMIT_RE = re.compile(r"^(submit|send|place order|sign in|log in|continue|next|pay|buy|purchase|sign up|register|create account)\b", re.I)
# Name heuristics for a textbox the DOM view did not describe: a field so named is treated as sensitive.
_PASSWORD_RE = re.compile(r"pass(word|code|wort|wd)|kennwort|mot de passe|contrase[ñn]a|senha|wachtwoord|パスワード|密码|密碼|비밀번호|\bpin\b", re.I)
_OTP_RE = re.compile(r"\botp\b|one[- ]?time|verification code|authenticat\w* code|auth code|sms code|2fa|two[- ]factor", re.I)
_CARD_RE = re.compile(r"card number|credit card|debit card|card holder|cardholder|\bcvv\b|\bcvc\b|security code|expir(y|ation)", re.I)


_DOM_KEY = re.compile(r"(?:^|\s)(\w[\w-]*)=")
_AX_INTERACTIVE = frozenset((
    "Button TextField TextArea SearchField SecureTextField CheckBox RadioButton PopUpButton MenuButton MenuItem MenuBarItem Link "
    "Row Cell Slider ComboBox Tab DisclosureTriangle Switch Stepper Incrementor").split())
_AX_ACTIONS = frozenset(("AXPress", "AXConfirm", "AXPick"))


def _dom_attrs(label):
    """`name=password id=password type=password value=x y` -> dict. `value=` is the last attribute and takes the rest of the
    label (it may hold spaces and fake `key=`s); of every other key the first occurrence counts. A page controls
    these strings, so a sensitive type/autocomplete anywhere in the label wins over a later or earlier decoy."""
    label = label or ""
    v = re.search(r"(?:^|\s)value=", label)
    head = label[:v.start()] if v else label
    bare = re.search(r"(?:^|\s)download(?=\s+\w[\w-]*=|\s*$)", head, re.I)     # <a download>: a boolean attribute has no `=`
    if bare:
        head = head[:bare.start()] + head[bare.end():]
    out, ms = {}, list(_DOM_KEY.finditer(head))
    for i, m in enumerate(ms):
        out.setdefault(m.group(1).lower(), head[m.end():ms[i + 1].start() if i + 1 < len(ms) else len(head)].strip())
    if v:
        out["value"] = label[v.end():].strip()
    if bare:
        out.setdefault("download", "")
    if re.search(r"(?:^|\s)type=password\b", label, re.I):
        out["type"] = "password"
    sens = [m.strip() for m in re.findall(r"(?:^|\s)autocomplete=(.*?)(?=\s\w[\w-]*=|$)", label, re.I | re.S) if _sensitive_ac(m)]
    if sens:                                                 # the value is a token list: `billing cc-number`
        out["autocomplete"] = sens[0].lower()
    return out


def _dom_class(node, attrs):
    t = attrs.get("type")
    if node == "button" or t in ("submit", "button", "reset", "image"):
        return "button"
    return t if t in ("checkbox", "radio") else "text"


def _sem_class(role):
    if role in ("textbox", "searchbox", "spinbutton", "combobox") or role.startswith("input"):
        return "text"
    return role if role in ("checkbox", "radio", "button", "link") else None


def _dom_join(sem, dom):
    """Pair each interactive semantic ref with the DOM element it came from. By name first (case-insensitive, against
    aria-label/name/id/value/placeholder); then by order within a role class, but only when the leftovers on both
    sides are equally many (honeypot inputs and icon buttons would otherwise shift every pairing). {index: attrs}"""
    cands = []
    for r in (dom or {}).get("refs") or []:
        if r.get("node") in ("input", "button", "textarea"):
            a = _dom_attrs(r.get("label"))
            if a.get("type") != "hidden":
                cands.append((_dom_class(r["node"], a), a))
        elif r.get("node") == "a":                            # anchors, for a link's href and download (best effort: they carry no text)
            cands.append(("link", _dom_attrs(r.get("label"))))
    used, got = set(), {}
    for i, s in enumerate(sem):
        c, nm = _sem_class(s["role"]), (s.get("name") or "").strip().lower()
        if c and nm:
            for j, (k, a) in enumerate(cands):
                if j not in used and k == c and nm in (a.get(f, "").strip().lower() for f in ("aria-label", "name", "id", "value", "placeholder", "title")):
                    used.add(j); got[i] = a
                    break
    for c in ("text", "button", "checkbox", "radio", "link"):
        left_s = [i for i, s in enumerate(sem) if i not in got and _sem_class(s["role"]) == c]
        left_d = [j for j, (k, _) in enumerate(cands) if j not in used and k == c]
        if left_s and len(left_s) == len(left_d):
            for i, j in zip(left_s, left_d):
                got[i] = cands[j][1]
    return got


def _on(v):
    return v not in (None, False, 0, "", "false", "0", "no")


def _dom_sensitive(dom):
    """True when the DOM view lists a visible password, one-time-code or card input."""
    for r in (dom or {}).get("refs") or []:
        if r.get("node") == "input":
            a = _dom_attrs(r.get("label"))
            if a.get("type") != "hidden" and (a.get("type") == "password" or _sensitive_ac(a.get("autocomplete"))):
                return True
    return False


def _guess_autocomplete(name):
    if _OTP_RE.search(name):
        return "one-time-code"
    m = _CARD_RE.search(name)
    return None if not m else "cc-csc" if re.search(r"cvv|cvc|security", m.group(0), re.I) else "cc-exp" if m.group(0).lower().startswith("expir") else "cc-number"


def _facts(role, name, input_type=None, autocomplete=None, in_form=False, is_submit=False, href=None, box=None, sensitive_unknown=False, download=False):
    return {"role": role, "name": name, "input_type": input_type, "autocomplete": autocomplete,
            "in_form": in_form, "is_submit": is_submit, "href": href, "download": download, "box": box,   # href/download: a link's DOM anchor, when joined
            "sensitive_unknown": sensitive_unknown}              # editable, not matched to the DOM, on a page whose DOM holds a sensitive input


VIEW_REPEATED_HEAD = ("Repeated controls (their position on the page is uncertain \u2014 open the item's own page or use a uniquely named "
                      "control; don't guess):")


def _assemble(header, items, budget, start=0):
    """items (document order): {kind: "i"|"t", vis, line: fn(id) -> str, ref, facts}. Keeps interactive-and-visible, then
    visible text, then off-screen interactive, then off-screen text, until the budget is spent; ids e1..eN (counting on from
    e<start>, for a later part of a long page) are then handed out in document order to the interactive items that made it.
    Items marked `rep` (controls whose name repeats, listed after the page) come under VIEW_REPEATED_HEAD whenever any is kept."""
    total = sum(1 for it in items if it["kind"] == "i")
    idw = len(str(max(start + total, 1)))
    room = int(budget * 3.2) - len("\n".join(header)) - 1 - 32           # 32 = the "(+N more not shown)" footer
    if any(it.get("rep") for it in items):
        room -= len(VIEW_REPEATED_HEAD) + 1
    chosen = set()
    for tier in ((True, "i"), (True, "t"), (False, "i"), (False, "t")):
        for n, it in enumerate(items):
            if (it["vis"], it["kind"]) != tier:
                continue
            cost = len(it["line"]("e" + "9" * idw)) + 1
            if cost > room:
                break
            room -= cost
            chosen.add(n)
        else:
            continue
        break
    lines, refs, headed = list(header), {}, False
    for n, it in enumerate(items):
        if n in chosen:
            if it.get("rep") and not headed:
                lines.append(VIEW_REPEATED_HEAD)
                headed = True
            eid = None
            if it["kind"] == "i":
                eid = f"e{start + len(refs) + 1}"
                refs[eid] = {"ref": it["ref"], "facts": it["facts"], "value": it.get("value")}
            lines.append(it["line"](eid))
    if len(chosen) < len(items):
        lines.append(f"(+{len(items) - len(chosen)} more not shown)")
    body = sum(len(it["line"]("e1")) for it in items)
    return {"text": "\n".join(lines), "refs": refs, "interactive": len(refs), "head": len(header),
            "sparse": total < 5 or body < 200}                             # judged on the whole snapshot, not on what fit


def _control_line(role, name, extras=""):
    return lambda eid: f"[{eid}] {role}" + (f' "{name}"' if name else "") + extras


def _view_controls(refs):
    return [r for r in refs or [] if r.get("role") not in _VIEW_TEXT_ROLES and r.get("role") not in _VIEW_PLAIN_ROLES]


def browser_view(state, dom=None, budget=6000, start=0, part=1, joined=None):
    """The view of one part of a page's semantic snapshot (CuaDriver pages a long one); part > 1 is a later part, which the
    model has scrolled to, so all of it counts as visible. Its eN numbers start after `start`. joined ({CuaDriver ref: DOM
    attrs}) is the DOM pairing made over every part read, so a control is never paired with another part's element."""
    refs = state.get("refs") or []
    sem = _view_controls(refs)
    if joined is None:
        pairs = _dom_join(sem, dom)
        dom_of = {id(s): pairs[i] for i, s in enumerate(sem) if i in pairs}
    else:
        dom_of = {id(s): joined[s.get("ref")] for s in sem if s.get("ref") in joined}
    dom_sens = _dom_sensitive(dom)
    key = lambda role, name: (role, " ".join((name or "").split()))        # noqa: E731
    groups = {}
    for r in sem:
        groups.setdefault(key(r.get("role"), r.get("name")), []).append(r)
    # A name that several controls share can't be placed: CuaDriver ranks refs by visibility, the outline is in document order,
    # so "the n-th Add to cart" in one is not the n-th in the other. Those controls are never paired with an outline line.
    repeated = {k: g for k, g in groups.items() if len(g) > 1}
    rep_ids, rep_form = {id(r) for g in repeated.values() for r in g}, {}
    sem_ids = {id(r) for r in sem} - rep_ids
    # walk the outline for document order, text and <form> scope; refs of unique names are matched to outline lines in order
    seq, ptr, forms, vis, form_at = [], 0, [], "in_viewport", None
    names = {" ".join((r.get("name") or "").split()) for r in sem}
    for raw in (state.get("outline") or "").splitlines():
        m = _OUTLINE_LINE.match(raw)
        if not m:
            continue
        ind, role, name = len(m.group(1)), m.group(2), " ".join((m.group(3) or "").split())
        # in_form is lenient: the outline's indentation drifts, so a form runs until a shallower line, a landmark or the next form
        if forms and ind < forms[-1]:
            forms = []
        if role == "form":
            forms = [ind]                                    # forms do not nest; a new one ends the last
        elif role in _VIEW_LANDMARKS:
            forms = []
        if role not in _VIEW_TEXT_ROLES and role not in _VIEW_PLAIN_ROLES and (role, name) in repeated:
            seq.append(("r", role, name, vis))               # the line stays (it tells where such controls are), with no number
            rep_form[(role, name)] = rep_form.get((role, name), False) or bool(forms)
            continue
        if role in _VIEW_TEXT_ROLES or role not in _VIEW_PLAIN_ROLES:
            hit = next((j for j in range(ptr, min(ptr + 60, len(refs)))
                        if refs[j].get("role") == role and " ".join((refs[j].get("name") or "").split()) == name), None)
            if hit is not None:
                for r in refs[ptr:hit]:
                    if id(r) in sem_ids:
                        seq.append(("i", r, bool(forms)))
                ptr = hit + 1
            if role in _VIEW_TEXT_ROLES:
                if name and name not in names and (not seq or seq[-1][:2] != ("t", name)):
                    seq.append(("t", name, role, vis))
            elif hit is not None:
                seq.append(("i", refs[hit], bool(forms)))
                vis = refs[hit].get("visibility", vis)
    for r in refs[ptr:]:
        if id(r) in sem_ids:
            seq.append(("i", r, False))
    for k, g in repeated.items():                            # then every repeated control, apart, in CuaDriver's order
        for i, r in enumerate(g):
            seq.append(("i", r, rep_form.get(k, False), f" ({i + 1} of {len(g)})"))
    items = []
    for e in seq:
        if e[0] == "t":
            kind = "heading" if e[2] == "heading" else "text"
            # Keep paragraph tails available to reading and query matching. Bounded chunks
            # let the view budget trim long text without dropping an entire paragraph.
            chunks = [_clip(e[1], 160)] if kind == "heading" else textwrap.wrap(e[1], width=600, break_on_hyphens=False)
            for chunk in chunks:
                items.append({"kind": "t", "vis": part > 1 or e[3] == "in_viewport",
                              "line": (lambda t, n: lambda eid: f'{t} "{n}"')(kind, chunk)})
            continue
        if e[0] == "r":
            items.append({"kind": "t", "vis": part > 1 or e[3] == "in_viewport", "line": (lambda t, n: lambda eid: t + (f' "{_clip(n, 100)}"' if n else ""))(e[1], e[2])})
            continue
        r, in_form, nth = e[1], e[2], (e[3] if len(e) > 3 else None)
        a = dom_of.get(id(r)) or {}
        role, name = r["role"], _clip(r.get("name"), 100)
        editable = _sem_class(role) == "text" or "type" in (r.get("actions") or ()) and role not in ("button", "link", "checkbox", "radio", "menuitem", "tab", "switch")
        link = role == "link"
        itype = None if link else a.get("type")                # <a type="application/pdf"> is no input type
        if editable and itype not in ("password", "search") and _PASSWORD_RE.search(name):      # a "show password" toggle turns type=password into text
            itype = "password"
        ac = a.get("autocomplete")
        if editable and not _sensitive_ac(ac):               # the label is the fallback unless the DOM already says it is sensitive
            ac = _guess_autocomplete(name) or ac
        unknown = bool(editable and (dom_sens or part > 1) and id(r) not in dom_of)     # a later part's field may be beyond what the DOM read held
        submit = itype == "submit" or (role == "button" and itype is None and (in_form or bool(_SUBMIT_RE.match(name))))
        st = r.get("states") or {}
        flags = [f for f in _VIEW_FLAGS if _on(st.get(f))] + (["checked" if _on(st["checked"]) else "unchecked"] if "checked" in st else [])
        extra = (f" type={itype}" if itype and itype not in ("text", "submit", "button") else "") + (f" [{' '.join(flags)}]" if flags else "")
        val = "" if r.get("value") in (None, "") else None           # None: it holds something that is withheld from the view
        if r.get("value") not in (None, "") and itype != "password" and not unknown and not _sensitive_ac(ac):
            extra += f' = "{_clip(r["value"], 80)}"'
            val = str(r["value"])
        items.append({"kind": "i", "vis": part > 1 or r.get("visibility") == "in_viewport", "ref": r["ref"], "line": _control_line(role, name, extra + (nth or "")),
                      "value": val, "rep": nth is not None,
                      "facts": _facts(role, r.get("name"), itype, ac, in_form, submit, href=a.get("href") if link else None,
                                      sensitive_unknown=unknown, download=link and "download" in a)})
    page = state.get("page") or {}
    title = page.get("title")
    if not title or title == "about:blank":                  # the root web area carries the real title
        title = next((c.get("name") for c in state.get("content_refs") or [] if c.get("role") == "rootwebarea" and c.get("name")), title)
    header = [f"Page: {_clip(title, 120)}", f"URL: {_clip(page.get('url'), 200)}"]
    if _snap_more(state):
        header.append(f"Part {part} of the page; scroll down for more")
    elif part > 1:
        header.append(f"Part {part} of the page; end of the page")
    return _assemble(header, items, budget, start)


def _snap_more(state):
    """The continuation token for the next part of this snapshot, or None at its end."""
    snap = state.get("snapshot") or {}
    tok = snap.get("continuation")
    return tok if snap.get("complete") is False and isinstance(tok, str) and tok else None


_QUERY_STOP = frozenset(("the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "is", "are", "best", "page"))


def _query_words(q):
    """The words of a read_page query worth looking for: lowercase, at least 2 characters, no stopwords."""
    out = []
    for w in re.split(r"[^a-z0-9]+", str(q or "").lower()):
        if len(w) >= 2 and w not in _QUERY_STOP and w not in out:
            out.append(w)
    return out


def _line_score(line, words):
    """How many of the query words a view line has (a plural or singular form counts too); the [eN] role prefix is not part
    of what is matched."""
    body = re.sub(r"^(?:\[e\d+\]\s+)?[\w-]+\s*", "", line).lower()
    have = {t for t in re.split(r"[^a-z0-9]+", body) if t}
    return sum(1 for w in words if have & {w, w + "s", w + "es"} or w.endswith("s") and w[:-1] in have)


def app_view(state, budget=6000):
    els = state.get("elements") or []
    wb = state.get("window_bounds") or {}
    sw, sh = state.get("screenshot_width"), state.get("screenshot_height")
    sx = sw / wb["width"] if sw and wb.get("width") else state.get("screenshot_scale") or 1.0
    sy = sh / wb["height"] if sh and wb.get("height") else state.get("screenshot_scale") or 1.0
    kids = {}
    for e in els:
        kids.setdefault(e.get("parent_index"), []).append(e)

    def inner(e, depth=3):                                   # the first text under a row or cell, which is what names it
        for c in kids.get(e["element_index"], ()):
            t = c.get("label") or c.get("value") if c.get("role") in ("AXStaticText", "AXTextField", "AXTextArea") else None
            if t or depth > 1 and (t := inner(c, depth - 1)):
                return t
        return None
    items = []
    for e in els:
        role = e.get("role") or ""
        short = role[2:] if role.startswith("AX") else role
        label, value, f = e.get("label"), e.get("value"), e.get("frame")
        if short in ("Row", "Cell") and not (label or value):
            label = inner(e)
        if short == "StaticText":
            t = value or label
            if t:
                items.append({"kind": "t", "vis": True, "line": (lambda n: lambda eid: f'text "{_clip(n, 160)}"')(t)})
            continue
        if not (short in _AX_INTERACTIVE or _AX_ACTIONS & set(e.get("actions") or ())):
            continue
        if not (label or value or f) or short in ("Row", "Cell") and not (label or value):
            continue                                         # nothing to call it by (and, for controls, nowhere to see it)
        box = [round((f["x"] - wb.get("x", 0)) * sx), round((f["y"] - wb.get("y", 0)) * sy), round(f["w"] * sx), round(f["h"] * sy)] if f else None
        vis = bool(box and sw and sh and box[0] < sw and box[1] < sh and box[0] + box[2] > 0 and box[1] + box[3] > 0)
        name = _clip(label or (value if short not in ("TextField", "TextArea", "SearchField", "SecureTextField", "ComboBox") else ""), 100)
        flags = [k for k in ("selected",) if e.get(k)] + (["disabled"] if e.get("enabled") is False else [])
        extra = (f" [{' '.join(flags)}]" if flags else "") + (f' = "{_clip(value, 80)}"' if value and label and short != "SecureTextField" else "")
        kind = short.lower()
        secure = short == "SecureTextField"
        if secure:
            kind = "textfield"
        items.append({"kind": "i", "vis": vis, "line": _control_line(kind, name, extra),
                      "ref": {"element_index": e["element_index"], "window_id": state.get("window_id"), "pid": state.get("pid"),
                              "element_token": e.get("element_token"), "snapshot_id": state.get("snapshot_id")},
                      "facts": _facts(kind, label or value, "password" if secure else None, None, False,
                                      kind == "button" and bool(_SUBMIT_RE.match(label or "")), None, box)})
    header = [f"App: {_clip(state.get('app_name'), 80)}" + (f' - window "{_clip(state.get("window_title"), 100)}"' if state.get("window_title") else "")]
    return _assemble(header, items, budget)


def fold_history(lines, budget=3000, label="Earlier", line_cap=400):
    """Newest steps stay whole (each capped); the oldest become one `Earlier: …` line. Fits est_tokens(...) <= budget."""
    cap = int(budget * 3.2)
    if est_tokens("\n".join(lines)) <= budget:
        return list(lines)
    head = max(40, cap // 5)
    room = cap - head - 1
    keep, used = [], 0
    for ln in reversed(lines):
        ln = _clip(ln, min(line_cap, room))
        if keep and used + len(ln) + 1 > room:
            break
        keep.append(ln); used += len(ln) + 1
    older = lines[:len(lines) - len(keep)]
    out = list(reversed(keep))
    if older:
        out.insert(0, _clip(f"{label}: " + "; ".join(_clip(l, 50) for l in older), head))
    return out
