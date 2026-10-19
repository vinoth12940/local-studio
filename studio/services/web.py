"""Public web search and page reads (public-IP pinned).

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import ipaddress
import re
import socket
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
            if _url_scheme(link) in ("http", "https") and _host(link) and not _local_kind(_host(link)):
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
