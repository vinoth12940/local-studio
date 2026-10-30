"""Public web search and page reads (public-IP pinned).

Moved from app.py in the refactor's P1 split.
"""

import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin, parse_qs

import httpx

from studio.security.agent_gate import _host, _local_kind, _url_scheme


def _public_web_target(url):
    """Validate and pin one public destination. DNS is checked once and the connection uses that IP."""
    if not isinstance(url, str) or len(url) > 4000 or re.search(r"[\x00-\x20\\]", url):
        raise ValueError("Use a full public HTTP(S) URL")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) pages without credentials can be read")
    if parsed.port not in (None, 443 if parsed.scheme == "https" else 80) or _local_kind(parsed.hostname):
        raise ValueError("This Mac and local-network URLs cannot be read by web tools")
    addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(a).is_global or ipaddress.ip_address(a).is_multicast for a in addresses):
        raise ValueError("This page does not resolve exclusively to public addresses")
    address = sorted(addresses, key=lambda a: (":" in a, a))[0]
    original = httpx.URL(url)
    return original.copy_with(host=address), {"Host": original.netloc.decode("ascii"), "User-Agent": "LocalStudio/1.4.2", "Accept": "text/html, application/xhtml+xml, text/plain, application/rss+xml, text/xml"}, original.host


def _public_web_request(url):
    target, headers, host = _public_web_target(url)
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", target, headers=headers, extensions={"sni_hostname": host}) as response:
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 1_500_000:
                    raise ValueError("Page is too large; read a smaller source page")
                chunks.append(chunk)
            return response.status_code, dict(response.headers), b"".join(chunks)


def _fetch_public_web(url):
    for _ in range(6):
        status, headers, content = _public_web_request(url)
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            url = urljoin(url, headers["location"])
            continue
        if status != 200:
            raise ValueError(f"The source returned HTTP {status}; its contents could not be verified")
        kind = headers.get("content-type", "").split(";", 1)[0].lower()
        if kind not in ("text/html", "application/xhtml+xml", "text/plain", "text/xml", "application/xml", "application/rss+xml"):
            raise ValueError("This source is not a readable text page; use the browser for it")
        return url, kind, content.decode("utf-8", errors="replace")
    raise ValueError("Too many page redirects")


class _WebPageText(HTMLParser):
    SKIP = frozenset(("script", "style", "noscript", "svg", "nav", "footer"))
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden, self.in_title, self.title, self.parts, self.size = [], False, [], [], 0
        self.dates = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        date_key = {"article:published_time": "published_at", "article:modified_time": "modified_at", "datepublished": "published_at", "datemodified": "modified_at"}.get((attrs.get("property") or attrs.get("name") or "").lower())
        if tag == "meta" and date_key and attrs.get("content"): self.dates[date_key] = attrs["content"][:200]
        if tag in self.SKIP: self.hidden.append(tag)
        if tag == "title": self.in_title = True
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "tr"): self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]: self.hidden.pop()
        if tag == "title": self.in_title = False

    def handle_data(self, value):
        if self.hidden or self.size > 50000: return
        if self.in_title: self.title.append(value)
        else:
            self.parts.append(value)
            self.size += len(value)

    def text(self):
        return "\n".join(" ".join(line.split()) for line in " ".join(self.parts).splitlines() if line.strip())


class _WebSearchResults(HTMLParser):
    """Read organic DuckDuckGo links and snippets; never treat ads or page chrome as sources."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results, self.field, self.parts = [], None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            link = urljoin("https://duckduckgo.com", attrs.get("href", ""))
            if _host(link) in ("duckduckgo.com", "www.duckduckgo.com"):
                link = parse_qs(urlsplit(link).query).get("uddg", [""])[0]
            if _url_scheme(link) in ("http", "https") and _host(link) and not _local_kind(_host(link)) \
                    and not _host(link).endswith("duckduckgo.com"):         # an ad's click-through (duckduckgo.com/y.js)
                self.results.append({"url": link, "title": "", "snippet": ""})
                self.field, self.parts = "title", []
        elif tag == "a" and "result__snippet" in classes and self.results:
            self.field, self.parts = "snippet", []

    def handle_endtag(self, tag):
        if tag == "a" and self.field:
            self.results[-1][self.field] = " ".join("".join(self.parts).split())[:900]
            self.field, self.parts = None, []

    def handle_data(self, value):
        if self.field: self.parts.append(value)


_DATE_META = {"article:published_time": "published_at", "article:modified_time": "modified_at", "datepublished": "published_at", "datemodified": "modified_at"}
_BLOCKS = frozenset(("p", "div", "li", "h1", "h2", "h3", "h4", "tr", "br", "section", "article", "main", "td", "blockquote"))


class _MainText(HTMLParser):
    """A page's article text in blocks: inside <article>/<main> when the page has one, never menus, headers or footers."""
    SKIP = frozenset(("script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "button", "select", "template", "iframe"))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = self.scope = self.link = 0
        self.in_title, self.title, self.dates, self.blocks, self.size = False, [], {}, [], 0
        self.has_scope = False
        self.block = {"text": [], "link": 0, "scoped": False, "heading": False}

    def _flush(self, heading=False):
        if "".join(self.block["text"]).strip():
            self.blocks.append(self.block)
        self.block = {"text": [], "link": 0, "scoped": False, "heading": heading}
        self.link = 0                                # an unclosed <a> never turns the rest of the page into link text

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        date_key = _DATE_META.get((attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower())
        if tag == "meta" and date_key and attrs.get("content"): self.dates.setdefault(date_key, attrs["content"][:200])
        if tag in self.SKIP: self.hidden += 1
        if tag == "title": self.in_title = True
        if tag in ("article", "main"):
            self.scope += 1
            self.has_scope = self.has_scope or not self.hidden     # an <article> card inside a hidden <aside> is not the page
        if tag == "a" and attrs.get("href"): self.link += 1
        if tag in _BLOCKS: self._flush(heading=tag in ("h1", "h2", "h3"))

    def handle_endtag(self, tag):
        if tag in self.SKIP: self.hidden = max(0, self.hidden - 1)
        if tag == "title": self.in_title = False
        if tag in ("article", "main"): self.scope = max(0, self.scope - 1)
        if tag == "a": self.link = max(0, self.link - 1)
        if tag in _BLOCKS: self._flush()

    def handle_data(self, value):
        if self.hidden or self.size > 200000: return            # before the title: an <svg><title> is an icon's name
        if self.in_title: self.title.append(value); return
        self.block["text"].append(value)
        self.block["scoped"] = self.block["scoped"] or self.scope > 0
        if self.link: self.block["link"] += len(value.strip())
        self.size += len(value)


def main_text(html, limit):
    """The readable article of a page, capped at `limit` characters, with its title and published/modified dates.

    Blocks that are mostly link text, and short lines with no number (menus, buttons, bylines of one word), are dropped;
    headings stay. Raises ValueError when almost nothing readable is left (an app shell, a sign-in wall)."""
    parser = _MainText()
    parser.feed(html)
    parser.close()
    parser._flush()

    def readable(scoped_only):
        lines, seen = [], set()
        for block in parser.blocks:
            if scoped_only and not block["scoped"]:
                continue
            text = " ".join("".join(block["text"]).split())
            if not text or block["link"] > len(text) / 2:
                continue
            if not block["heading"] and len(text.split()) < 5 and not re.search(r"\d", text):
                continue
            if text not in seen:
                seen.add(text)
                lines.append(text)
        return "\n".join(lines)

    text = readable(parser.has_scope)
    if parser.has_scope and len(text) < 80:              # a small <main> hero with the story outside it: read the whole page
        text = readable(False)
    if len(text) < 80:
        raise ValueError("The page has too little readable text. It may require JavaScript or sign-in; use the browser or another source")
    return {"title": " ".join("".join(parser.title).split()), "text": text[:limit], "truncated": len(text) > limit, **parser.dates}


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element, name):
    return next((c for c in element if _local(c.tag) == name), None)


def _words(element):
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _utc(value, parse):
    """An ISO 8601 UTC time ("…Z") from a feed date, or None when it is missing or unreadable."""
    try:
        moment = parse(value.strip())
    except (TypeError, ValueError, IndexError, AttributeError, OverflowError):
        return None
    if moment is None:
        return None
    moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    try:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OverflowError:                            # year 1 in a timezone east of UTC
        return None


def feed_items(xml):
    """RSS 2.0, RSS 1.0 and Atom items as {title, url, source, published_at}; published_at is None when the feed gives no
    readable date. Python's expat refuses entity-expansion bombs and ElementTree never fetches external entities."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise ValueError("This feed could not be read") from error
    container = _child(root, "channel") if _local(root.tag) == "rss" else root
    feed_title = _words(_child(container, "title")) if container is not None else ""
    items = []
    for element in root.iter():
        kind = _local(element.tag)
        if kind not in ("item", "entry"):
            continue
        if kind == "entry":
            links = [c for c in element if _local(c.tag) == "link"]
            link = next((c for c in links if c.get("rel") in (None, "alternate")), links[0] if links else None)
            url = (link.get("href") or "").strip() if link is not None else ""
            stamp = _child(element, "published") if _child(element, "published") is not None else _child(element, "updated")
            published = _utc(stamp.text if stamp is not None else None, datetime.fromisoformat)
        else:
            url = _words(_child(element, "link"))
            stamp = _child(element, "pubDate") if _child(element, "pubDate") is not None else _child(element, "date")
            parse = parsedate_to_datetime if stamp is not None and _local(stamp.tag) == "pubDate" else datetime.fromisoformat
            published = _utc(stamp.text if stamp is not None else None, parse)
        items.append({"title": _words(_child(element, "title")), "url": url,
                      "source": _words(_child(element, "source")) or feed_title, "published_at": published})
    return items
