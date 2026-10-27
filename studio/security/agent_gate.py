"""The computer-use approval gate: safe / risky / blocked from facts about the target.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import ipaddress
import re
import unicodedata
from urllib.parse import unquote, unquote_plus, urlsplit

from studio.infra.text import _clip


_SENSITIVE_AC = re.compile(r"^(current-password|new-password|one-time-code|cc-.*)$", re.I)


def _sensitive_ac(v):
    """True when any whitespace-separated autocomplete token is a password, one-time-code or card one
    (`billing cc-number`, `section-x cc-number`, `current-password webauthn`)."""
    return any(_SENSITIVE_AC.match(t) for t in str(v or "").split())


# ---- approval gate: decides from facts about the target (never the model's own words) whether an action is safe, risky or blocked

# Words on a control (or in a menu path) that make acting on it irreversible or public. Matched after _norm_label, with
# the inflections people see on buttons ("Sends", "Deleting...", "Payment"); \s* / \s+ inside the multi-word ones.
RISKY_WORDS = re.compile(
    r"\b(?:send(?:s|ing)?|sent|submit(?:s|ted|ting)?|buy(?:s|ing)?|pay(?:s|ing|ment|ments|ed)?|order(?:s|ed|ing)?|"
    r"check[\s\-‐-―−]*out|confirm(?:s|ed|ing|ation)?|delet(?:e|es|ed|ing|ion)|remov(?:e|es|ed|ing|al)|post(?:s|ed|ing)?|"
    r"publish(?:es|ed|ing)?|sign[\s\-‐-―−]*up|(?:un)?subscri(?:be|bes|bed|bing|ption|ptions)|book(?:s|ed|ing)?|transfer(?:s|red|ring)?|"
    r"place[\s\-‐-―−]*order|purchas(?:e|es|ed|ing)|donat(?:e|es|ed|ing|ion|ions)|trash(?:ed|ing)?|repl(?:y|ies|ied|ying)|"
    r"shar(?:e|es|ed|ing)|authori[sz](?:e|es|ed|ing|ation)|approv(?:e|es|ed|ing|al)|accept(?:s|ed|ing)?|grant(?:s|ed|ing)?)\b", re.I)
APP_TIERS_DEFAULT = {
    **{n: "read" for n in (
        "Keychain Access", "System Settings", "Terminal", "iTerm", "Warp", "1Password", "Bitwarden", "Passwords",
        "Chase", "PayPal", "Venmo", "Coinbase", "Robinhood", "Wise", "Revolut", "Schwab", "Fidelity", "Vanguard",
        "E*TRADE", "Binance", "Kraken", "Quicken", "YNAB",
        "LastPass", "Dashlane", "Keeper", "Enpass", "KeePassXC", "Proton Pass",
        "Ghostty", "kitty", "Alacritty", "WezTerm", "Hyper", "Tabby",
        "Shortcuts", "Script Editor", "Automator",
        "Local Studio")},                                    # the agent never drives the app that runs it
    **{n: "ask" for n in (
        "Mail", "Messages", "WhatsApp", "Slack", "Calendar", "Finder",
        "Telegram", "Discord", "Signal", "Microsoft Teams", "Skype", "LINE", "WeChat", "Zoom", "zoom.us", "Outlook", "Microsoft Outlook",
        # the user's own browsers hold their sign-ins and saved cards; the agent browses in its own isolated Chrome (the browser surface)
        "Safari", "Google Chrome", "Firefox", "Arc", "Microsoft Edge", "Brave", "Opera", "Vivaldi")},
}
APP_TIERS_BUNDLE_DEFAULT = {                                  # the same defaults by bundle id, which a localized macOS ("Systemeinstellungen") does not change
    **{b: "read" for b in (
        "com.apple.systempreferences", "com.apple.keychainaccess", "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable",
        "com.mitchellh.ghostty", "com.apple.ScriptEditor2", "com.apple.Automator", "com.apple.shortcuts", "com.1password.1password",
        "com.bitwarden.desktop", "com.apple.Passwords", "com.localstudio.app")},
    **{b: "ask" for b in (
        "com.apple.mail", "com.apple.MobileSMS", "net.whatsapp.WhatsApp", "com.tinyspeck.slackmacgap", "com.apple.iCal", "com.apple.finder",
        "ru.keepcoder.Telegram", "com.hnc.Discord", "org.whispersystems.signal-desktop", "com.microsoft.teams2", "com.microsoft.Outlook", "us.zoom.xos",
        "com.apple.Safari", "com.google.Chrome", "org.mozilla.firefox", "company.thebrowser.Browser", "com.microsoft.edgemac",
        "com.brave.Browser", "com.operasoftware.Opera", "com.vivaldi.Vivaldi", "org.chromium.Chromium", "com.kagi.kagimacOS",
        "app.zen-browser.zen", "company.thebrowser.dia")},
}
READ_ONLY_NAME = re.compile(r"bank|banking|finance|wallet|password|crypto|invest|broker|trading|pay\b", re.I)
SEARCH_DOMAINS = frozenset(("duckduckgo.com", "google.com", "bing.com"))     # open_url may go to a search on exactly these hosts (_search_ok)
_TIER_NAMES = ("read", "ask", "allowed")
_SEARCH_FIRST_RE = re.compile(r"^\s*(?:search|find)\b", re.I)
_SEARCH_HINT_RE = re.compile(r"\b(?:enter|type)\s+(?:your\s+)?search\b|\bsearch\s+(?:here|for|the|this|google|bing|duckduckgo|the\s+web)\b", re.I)
_COMPOSER_RE = re.compile(r"^\s*(?:message|reply|comment|chat|post|write|send|compose)\b", re.I)    # a message box is never a search box
_SEARCH_ROLES = frozenset(("searchbox", "searchfield"))
_CTRL_RE = re.compile("[\\x00-\\x08\\x0a-\\x1f\\x7f\\u2028\\u2029\\x85]")      # C0 controls but tab (Enter, NSEnter \\x03, Backspace, ...) and line separators
_ENTER_KEYS = frozenset(("↩", "⏎", "⌅", "↵", "space", "spacebar", "␣", "kpenter", "newline", "linefeed", "lf", "cr"))
_DELETE_KEYS = frozenset(("del", "⌫", "⌦", "kpdelete"))
_APP_ACTING = frozenset(("click_app", "type_app", "menu", "hotkey"))
_SETTABLE = frozenset(("popupbutton", "combobox", "slider", "incrementor"))            # type_app sets these with set_value: a choice, not typing
_TEXT_ROLES = frozenset(("textfield", "textarea", "searchfield")) | {"combobox"}       # type_app types into these with type_text
_KEY_SPLIT = re.compile(r"[+\s]+|(?<=\w)-(?=\w)")
_TYPING = frozenset(("type", "type_app"))
_SAFE_TOOLS = frozenset(("read_page", "scroll", "back", "note", "ask_user", "done", "list_apps", "read_app"))
_MD_SPECIAL = re.compile(r"([\\*_\[\]()!`#<>|&~])|:(?=//)")


def _norm_label(s):
    """What a person would read: NFKC, no zero-width/bidi (Cf) or control characters, whitespace collapsed."""
    s = unicodedata.normalize("NFKC", str(s if s is not None else ""))
    s = "".join(" " if unicodedata.category(c) == "Cc" else "" if unicodedata.category(c) == "Cf" else c for c in s)
    return " ".join(s.split())


def _visible(s):
    """Spell out format (Cf) and control characters as ‹U+200D› so a lookalike host cannot hide behind them."""
    return "".join(f"‹U+{ord(c):04X}›" if unicodedata.category(c) in ("Cf", "Cc") else c for c in str(s if s is not None else ""))


def _md_escape(s):
    return _MD_SPECIAL.sub(lambda m: "\\" + m.group(0), s)


def _tier_key(name):
    k = _norm_label(name).casefold()                         # no invisible characters: "\u200eWhatsApp" is WhatsApp
    return k[:-4] if k.endswith(".app") else k


def _browser_name(app_name):
    """A Chromium fork or other browser the lists above don't know by name: "Tor Browser", "Chromium", "Some New Browser"."""
    k = _tier_key(app_name)
    return k == "chromium" or k.endswith("browser")


def _default_tier(k):
    """Longest default whose name equals k or is a whole-word prefix of it ("Microsoft Teams (work or school)", "Telegram Desktop")."""
    best = None
    for n, t in APP_TIERS_DEFAULT.items():
        d = _tier_key(n)
        if (k == d or k.startswith(d) and not k[len(d)].isalnum()) and (best is None or len(d) > best[0]):
            best = (len(d), t)
    return best[1] if best else None


def app_tier(app_name, overrides, bundle_id=None):
    """"read" (look only), "ask" (every action needs approval) or "allowed": the user's overrides (exact name, or bundle id) first,
    else the stricter of the default list (name or whole-word prefix), the same list by bundle id and the name rule (money or
    secrets: read); else allowed."""
    k, bid = _tier_key(app_name), str(bundle_id or "").casefold()
    for n, t in (overrides or {}).items():
        if t in _TIER_NAMES and (_tier_key(n) == k or bid and _tier_key(n) == bid):
            return t
    t = _default_tier(k) or (_default_tier(k.rstrip("0123456789 ")) if k.rstrip("0123456789 ") != k else None)   # iTerm2 -> iterm
    rule = "read" if READ_ONLY_NAME.search(_norm_label(app_name)) else None            # "Zoom Pay", "Mail Banking": the stricter of the two wins
    by_id = next((v for b, v in APP_TIERS_BUNDLE_DEFAULT.items() if b.casefold() == bid), None) if bid else None
    browser = "ask" if _browser_name(app_name) else None                              # an unlisted browser holds the user's sign-ins too
    return min((x for x in (t, rule, by_id, browser) if x), key=_TIER_NAMES.index, default="allowed")


def _norm_url(u):
    """The WHATWG way: leading/trailing C0 controls and spaces go, tab/CR/LF vanish everywhere."""
    return re.sub(r"[\t\r\n]", "", str(u if u is not None else "")).strip("".join(map(chr, range(0x21))))


def _url_scheme(u):
    """Any `name:` prefix of a normalised URL is a scheme (so `javascript:1/x` and `data:1/x` are); http/https are the only ones
    let through, so a schemeless `host:port` fails safe."""
    m = re.match(r"^([a-z][a-z0-9+.-]*):", u, re.I)
    return m.group(1).lower() if m else ""


def _split_url(s):
    return urlsplit(s if re.match(r"^(?:[a-z][a-z0-9+.-]*:)?//", s, re.I) else "//" + s)


def _host(s):
    """Lowercase ASCII (IDNA) host of a URL or bare host, without www., port or trailing dot; "" if there is none.
    A host with ß, ς, ZWNJ or ZWJ is returned as typed: Python's IDNA2003 would map ß to ss where browsers (UTS 46) keep it, so
    it must not be compared as its ASCII look-alike."""
    s = _norm_url(s)
    if not s or "\\" in s:                                         # browsers read \ as /, urlsplit does not: https://evil.net\@example.com
        return ""
    try:
        h = unquote(_split_url(s).hostname or "").lower().rstrip(".")      # a browser decodes %31 in a host: 127.0.0.%31 is 127.0.0.1
        if not any(c in h for c in "ßς\u200c\u200d"):
            h = h.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return ""
    return h[4:] if h.startswith("www.") else h


def _search_ok(url, host):
    """A plain web search on exactly google.com, bing.com or duckduckgo.com (www. allowed). The path must be exactly the
    search page (urlsplit does not resolve dot segments, browsers do, so nothing with a `%` or a `.` segment qualifies);
    not their subdomains, redirectors (/url, /l/, /ck/a), "I'm feeling lucky" (btnI) or bangs / `\\` queries (DuckDuckGo
    and Bing), which jump to other sites."""
    if host not in SEARCH_DOMAINS:
        return False
    try:
        parts = _split_url(url)
    except ValueError:
        return False
    path = parts.path.lower()
    if "%" in path or "\\" in url or ";" in parts.query:          # DuckDuckGo's backend splits on `;` too: x=1;q=!g would hide a bang
        return False
    qs = [_norm_label(unquote_plus(kv.partition("=")[2])) for kv in re.split(r"[&;]", parts.query)
          if unquote_plus(kv.partition("=")[0]).lower() == "q"]    # every q, decoded; fullwidth ！ / ＼ and a ZWSP before \ count
    if host != "google.com" and any("!" in v or v.startswith("\\") for v in qs):
        return False
    if host == "google.com":                                        # btnI anywhere in the query
        return path == "/search" and "btni" not in unquote(parts.query).lower()
    if host == "bing.com":
        return path == "/search"
    return path in ("", "/", "/html", "/html/") and any(qs)


def _ipv4_num(p):
    """One part of a host that a browser reads as an IPv4 number (decimal, 0x hex or leading-0 octal); None if it is not one."""
    try:
        if p[:2] in ("0x", "0X"):
            return int(p[2:] or "0", 16)
        if len(p) > 1 and p[0] == "0":
            return int(p, 8)
        return int(p, 10) if p.isdigit() else None
    except ValueError:
        return None


def _ipv4_whatwg(h):
    """The IPv4 address a browser makes of a host (127.1, 0x7f.1, 2130706433, 0177.0.0.1 are all 127.0.0.1), the WHATWG way:
    a host whose last label is a number is an address. None when it is not one; False when it ends in a number but is no valid
    address (the browser refuses it, and nothing may vouch for it)."""
    parts = h.split(".")
    if parts and parts[-1] == "" and len(parts) > 1:
        parts.pop()
    last = parts[-1] if parts else ""
    if not (last.isascii() and (last.isdigit() or re.fullmatch(r"0[xX][0-9a-fA-F]*", last))):
        return None
    nums = [_ipv4_num(p) for p in parts]
    if len(parts) > 4 or any(n is None for n in nums) or any(n > 255 for n in nums[:-1]) or nums[-1] >= 256 ** (5 - len(nums)):
        return False
    v = nums[-1]
    for i, n in enumerate(nums[:-1]):
        v += n << (8 * (3 - i))
    return ipaddress.IPv4Address(v)


def _local_kind(host):
    """"loopback" for an address served by this Mac (127/8, ::1, 0.0.0.0/8, ::, localhost and *.localhost, in any spelling a browser
    accepts), "private" for the local network (private, link-local, reserved, *.local) and any host that ends in a number but is
    no valid address; None for an ordinary public host. Neither kind is ever vouched for."""
    h = str(host or "").strip().lower().strip("[]").rstrip(".")
    if not h:
        return None
    if h == "localhost" or h.endswith(".localhost"):
        return "loopback"
    if h == "local" or h.endswith(".local"):
        return "private"
    try:
        ip = ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        ip = _ipv4_whatwg(h)
        if ip is None:
            return None
        if ip is False:
            return "private"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_unspecified or (ip.version == 4 and ip.packed[0] == 0):
        return "loopback"
    return None if ip.is_global else "private"


def _domain_in(host, domains):
    if _local_kind(host):                                           # this Mac and the local network never count as part of anything
        return False
    for d in domains or ():
        d = _host(d)
        if "." in d and not _local_kind(d) and (host == d or host.endswith("." + d)):   # a bare label ("com", "localhost") never vouches for anything
            return True
    return False


DOWNLOAD_EXT = frozenset((
    "zip 7z rar tar gz tgz bz2 xz dmg pkg mpkg iso img app exe msi apk deb rpm bin jar sh command bat ps1 py js "
    "pdf doc docx xls xlsx ppt pptx csv odt ods rtf epub mp3 wav flac mp4 mov avi mkv webm torrent").split())


def _file_href(href):
    """True when a link's href names a file a browser downloads (by the extension of its path's last segment)."""
    if not isinstance(href, str) or not href.strip():
        return False
    try:
        path = unquote(urlsplit(_norm_url(href)).path)             # relative, //host/... and scheme://host/... alike
    except ValueError:
        return False
    last = "" if path.endswith("/") else path.rsplit("/", 1)[-1]
    return "." in last and last.rsplit(".", 1)[1].lower() in DOWNLOAD_EXT


def _sensitive_field(facts):
    return (facts.get("input_type") or "").strip().lower() == "password" or _sensitive_ac(facts.get("autocomplete"))


def _key_class(keys):
    """"plain", "enter" (return/enter/space, any spelling), "delete" or "invalid" for a hotkey's `keys`: a list of strings
    (a lone string counts as one element), each element possibly a combo like `cmd+return` or `cmd-return`."""
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, (list, tuple)) or not keys or not all(isinstance(k, str) for k in keys):
        return "invalid"
    toks, cls = [], "plain"
    for k in keys:
        if _CTRL_RE.search(k):
            return "enter"
        if any(part and not part.strip(" ") for part in unicodedata.normalize("NFKC", k).split("+")):
            return "enter"                                          # a literal space key, `shift+ `
        toks += [t for t in _KEY_SPLIT.split(k) if t]
    if not toks:
        return "invalid"
    for t in toks:
        t = unicodedata.normalize("NFKC", t).lower().replace("_", "").replace("-", "")
        if t in _ENTER_KEYS or "enter" in t or "return" in t:
            return "enter"
        if t in _DELETE_KEYS or "delete" in t or "backspace" in t:
            cls = "delete"
    return cls


def _menu_path(path):
    """A menu path as a list of item names: a list of strings, or one string like `File > Delete Note`. None if it is neither."""
    if isinstance(path, str):
        path = [path]
    if not isinstance(path, (list, tuple)) or not path or not all(isinstance(p, str) for p in path):
        return None
    out = [_norm_label(x) for p in path for x in re.split(r"[>›]", p)]
    return [x for x in out if x] or None


def gate(tool, args, facts, ctx):
    """Decide `("safe" | "risky" | "blocked", reason)` for one action. Pure; reads only its arguments.

    tool   model-facing tool name (see _SAFE_TOOLS, _TYPING, _APP_ACTING, click, open_url), plus the adapter's own
           `download` (a download about to happen) and `dialog` (answering a page confirm/beforeunload dialog).
    args   the tool's arguments (`url` for open_url, `text`/`submit` for type, `keys` for hotkey, `app`/`path` for menu).
    facts  Facts of the target ref (None when there is none); a click/type without facts is risky.
    ctx    surface         "browser" | "app"; every acting tool is judged by the app's tier when it is "app"
           app, tier       the app being acted on and its tier ("read" | "ask" | "allowed"); a missing tier counts as "ask"
           domain          host of the current page (open_url to it is safe)
           task_domains    set of hosts the task is about
           link_domains    set of hosts of links seen on the page (from the DOM; facts["href"] is set only for a link joined to its anchor)
           url_from_page   not used: the model writes every URL, so where it came from cannot be known
    open_url is safe only within task_domains | link_domains | {domain} (a subdomain counts as its parent; a domain with no dot
    never counts) or for a plain search on SEARCH_DOMAINS (_search_ok); anything else is risky, any scheme but http(s) is blocked. Typing that presses Enter (submit=True, or a newline in
    the text) is risky unless the target is a search field. This Mac (loopback) is never opened and the local network always
    asks (_local_kind). A click on a link that downloads (download attribute, or a file href) is risky."""
    args = args if isinstance(args, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    facts = facts if isinstance(facts, dict) else None
    if tool in _SAFE_TOOLS:
        return "safe", "reading, scrolling and notes do not change anything"
    if tool == "open_app":
        t = ctx.get("target") if isinstance(ctx.get("target"), dict) else None
        if t and not t.get("running") and t.get("tier") in ("read", "ask"):
            return "risky", f"{_norm_label(t.get('name')) or 'this app'} is not running and is {'read-only' if t['tier'] == 'read' else 'an ask-first app'} for the agent"
        return "safe", "opening an app does not change anything in it"
    if tool == "download":
        return "risky", "a file is about to be downloaded"
    if tool == "dialog":
        return "risky", "the page is asking a question in a dialog"
    if tool == "open_url":
        url = _norm_url(args.get("url"))
        sch = _url_scheme(url)
        if re.match(r"^[a-z0-9.-]+:\d+(?:[/?#]|$)", url, re.I):
            return "blocked", "the address needs http:// or https:// in front"
        if sch and sch not in ("http", "https"):
            return "blocked", f"only http and https addresses can be opened, not {sch}:"
        host = _host(url)                                           # no scheme counts as https://<host>
        if not host:
            return "risky", "the address has no usable host"
        local = _local_kind(host)
        if local == "loopback":                                     # Local Studio's own chats, file previews and UI are served here
            return "blocked", "the agent never opens pages served by this Mac"
        if local:
            return "risky", f"{host} is on your local network"
        if (_domain_in(host, ctx.get("task_domains")) or _domain_in(host, ctx.get("link_domains"))
                or _domain_in(host, [ctx.get("domain")]) or _search_ok(url, host)):
            return "safe", f"{host} is part of the task, the page or a search engine"
        return "risky", f"{host} is not part of the task"
    if tool in _TYPING or tool == "click" or tool in _APP_ACTING:
        return _gate_acting(tool, args, facts, ctx)
    return "blocked", f"unknown tool {tool!r}"


def _is_search_field(facts, name):
    """A search box, whose Enter only runs a search: an AX/ARIA search role, a name that starts with search/find, or a
    placeholder like "Enter your search here". Never a message composer ("Message #search-team", "Reply to: ...search...")."""
    if _COMPOSER_RE.search(name):
        return False
    return (facts.get("role") or "").lower() in _SEARCH_ROLES or bool(_SEARCH_FIRST_RE.search(name) or _SEARCH_HINT_RE.search(name))


def _gate_acting(tool, args, facts, ctx):
    if tool in ("hotkey", "menu") and ctx.get("surface") == "browser":
        return "blocked", "hotkey and menu work only in Mac apps; use click or type on the page"
    in_app = ctx.get("surface") == "app" or tool in _APP_ACTING
    tier = (ctx.get("tier") if ctx.get("tier") in _TIER_NAMES else "ask") if in_app else None
    app = ctx.get("app")
    if tool in _TYPING and facts and _sensitive_field(facts):
        return "blocked", "passwords, one-time codes and card details are never typed by the agent"
    if tier == "read":
        return "blocked", f"{app or 'this app'} is read-only for the agent"
    if tier == "ask":
        if ctx.get("surface") != "app" and not ctx.get("tier"):
            return "risky", "keyboard shortcuts and app actions need approval when no app is chosen"
        return "risky", f"every action in {app or 'this app'} needs approval"
    if tool == "menu":
        if args.get("app") is not None and _tier_key(_norm_label(args.get("app"))) != _tier_key(_norm_label(app)):
            return "risky", f"the menu belongs to {_norm_label(args.get('app'))!r}, not {app or 'the chosen app'}"
        path = _menu_path(args.get("path"))
        if not path:
            return "risky", "the menu path is not understood"
        hit = next((p for p in path if RISKY_WORDS.search(p)), None)
        return ("risky", f"the menu item {hit!r} sounds irreversible") if hit else ("safe", "an ordinary menu item")
    if tool == "hotkey":
        k = _key_class(args.get("keys"))
        return {"invalid": ("risky", "the keys are not understood"), "enter": ("risky", "the keys include return, enter or space"),
                "delete": ("risky", "the keys include delete"), "plain": ("safe", "an ordinary shortcut")}[k]
    if not facts:
        return "risky", "the target could not be identified"
    name = _norm_label(facts.get("name"))
    if tool in _TYPING:
        if facts.get("sensitive_unknown"):
            return "risky", "this field may hold a password or code"
        if in_app and RISKY_WORDS.search(name):
            return "risky", f"the field {name!r} sounds irreversible"
        text = args.get("text")
        if tool == "type_app" and facts.get("role") in _SETTABLE and isinstance(text, str) and RISKY_WORDS.search(_norm_label(text)):
            return "risky", f"the choice {_clip(_norm_label(text), 60)!r} sounds irreversible"          # a pull-down can run "Delete…"
        if args.get("submit") or (isinstance(text, str) and _CTRL_RE.search(text)):
            if _is_search_field(facts, name):
                return "safe", "a search"
            return "risky", "typing would also press Enter"
        return "safe", "ordinary typing"
    if facts.get("is_submit"):
        return "risky", "this control submits a form"
    href = facts.get("href")
    if href and _url_scheme(_norm_url(href)) in ("http", "https", ""):
        local = _local_kind(_host(href))
        if local == "loopback":
            return "blocked", "the link goes to a page served by this Mac"
        if local:
            return "risky", "the link goes to your local network"
    if facts.get("download") or _file_href(href):
        return "risky", "this link downloads a file"
    if RISKY_WORDS.search(name):
        return "risky", f"{name!r} sounds irreversible"
    return "safe", "an ordinary control"


_KEY_NAMES = {"cmd": "⌘", "command": "⌘", "shift": "⇧", "option": "⌥", "alt": "⌥", "opt": "⌥", "ctrl": "⌃", "control": "⌃"}


def _card_host(h):
    """A host for the approval card: joiners and bidi marks spelled out, whole up to a DNS name's length; past that only the
    START is cut (leading …), never the registrable tail (x.evil.net)."""
    h = " ".join(_visible(h).split())
    keep = max(256, len(".".join(h.split(".")[-3:])))
    return _md_escape(h if len(h) <= keep else "…" + h[-keep:])


def _typed_text(tool, facts, args):
    """What the card says will be typed: the text, quoted, clipped, with invisible and control characters spelled out and
    markdown escaped; for type_app each new line (a Return press) shows as ↵. Never the text of a field that may hold a password,
    code or card number, or of a target that could not be identified (the same rule as the run's own redaction)."""
    text = args.get("text")
    if not isinstance(text, str):
        return ""
    if not facts or _sensitive_field(facts) or facts.get("sensitive_unknown"):
        return "the text you can't see "
    if tool == "type_app":
        text = " ↵ ".join(x for x in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if x)
    return f"“{_md_escape(_clip(_visible(text), 80))}” "


def _typed_keys(tool, args):
    """" and press Enter" / " and press Return N×" when typing also submits, exactly when the gate says it does."""
    text = args.get("text")
    if tool == "type_app" and isinstance(text, str):
        n = text.replace("\r\n", "\n").replace("\r", "\n").count("\n")
        if n:
            return f" and press Return {n}×"
    if args.get("submit") or (isinstance(text, str) and _CTRL_RE.search(text)):
        return " and press Enter"
    return ""


def approval_title(tool, facts, ctx, args=None):
    """One line for the approval card, in the user's terms. Labels come from the facts, not from the model; everything a
    page or app controls is cleaned (_norm_label) and markdown-escaped so it cannot rewrite the card."""
    args, ctx = args or {}, ctx or {}
    esc = lambda v, n=80: _md_escape(_clip(_norm_label(v), n))
    where_web = f"on {_card_host(ctx.get('domain') or '') or 'this page'}"
    where_app = "" if ctx.get("surface") == "browser" and not ctx.get("app") else f"in {esc(ctx.get('app') or 'the app')}"   # never "in the app" on a web page
    f = facts or {}
    label = lambda: esc(f.get("name") or f.get("role") or "control")
    if tool == "click":
        return f"Click **{label()}** {where_web}"
    if tool == "click_app":
        return f"Click **{label()}** {where_app}".rstrip()
    if tool == "type":
        return f"Type {_typed_text(tool, facts, args)}into **{label()}** {where_web}{_typed_keys(tool, args)}"
    if tool == "type_app":
        if f.get("role") in _SETTABLE:
            return f"Choose **{esc(args.get('text'))}** in **{label()}** {where_app}".rstrip()
        return f"Type {_typed_text(tool, facts, args)}into **{label()}** {where_app}".rstrip() + _typed_keys(tool, args)
    if tool == "open_app":
        return f"Open **{esc((ctx.get('target') or {}).get('name') or args.get('name') or 'an app')}**"
    if tool == "download":
        return f"Download **{esc(f.get('name') or args.get('name') or args.get('filename') or 'a file')}** {where_web}"
    if tool == "dialog":
        return f"Answer a page dialog {where_web}"
    if tool == "open_url":
        url = _norm_url(args.get("url"))
        show = lambda v, n=80: _md_escape(_clip(_visible(v), n))       # joiners and bidi marks are spelled out, not hidden
        return f"Open **{_card_host(_host(url)) or 'an address with no host'}** ({show(url)})"
    if tool == "menu":
        return f"Use menu **{esc(' › '.join(_menu_path(args.get('path')) or ['?']))}** {where_app}".rstrip()
    if tool == "hotkey":
        ks = args.get("keys")
        parts = [t for k in ([ks] if isinstance(ks, str) else ks if isinstance(ks, (list, tuple)) else ()) if isinstance(k, str)
                 for t in _KEY_SPLIT.split(_norm_label(k)) if t]
        keys = [_KEY_NAMES.get(t.lower(), t.capitalize() if len(t) > 1 else t.upper()) for t in parts]
        return f"Press **{esc('+'.join(keys) or '?')}** {where_app}".rstrip()
    return f"Run **{esc(tool)}**"
