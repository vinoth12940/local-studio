// Build guard for the chat markdown sanitizer (frontend/core/markdown.mjs), run under plain node without a browser DOM:
//   node macos/check_sanitizer.mjs
// cleanChat() walks a parsed reply and keeps an allowlist; it is driven here through a tiny element tree that has only what
// it uses. A browser has already decoded entities in attribute values when cleanChat sees them, so the cases below pass
// decoded values (and safeMd(), which sees raw markdown text, gets the entity-encoded forms).
import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

class Text {
  constructor(data) { this.data = data; this.parent = null; }
}
class El {
  constructor(tag, attrs = {}, children = []) {
    this.localName = tag; this.parent = null; this.childNodes = []; this._a = new Map(Object.entries(attrs));
    for (const c of children) this.append(typeof c === "string" ? new Text(c) : c);
  }
  get attributes() { return [...this._a.keys()].map((name) => ({ name })); }
  getAttribute(n) { return this._a.has(n) ? this._a.get(n) : null; }
  setAttribute(n, v) { this._a.set(n, String(v)); }
  removeAttribute(n) { this._a.delete(n); }
  append(c) { c.parent = this; this.childNodes.push(c); }
  remove() { if (this.parent) { this.parent.childNodes = this.parent.childNodes.filter((x) => x !== this); this.parent = null; } }
  replaceWith(...nodes) {
    const p = this.parent, i = p.childNodes.indexOf(this);
    for (const n of nodes) n.parent = p;
    p.childNodes.splice(i, 1, ...nodes); this.parent = null;
  }
  querySelectorAll(sel) {
    assert.equal(sel, "*");
    const out = [], walk = (n) => { for (const c of n.childNodes) if (c instanceof El) { out.push(c); walk(c); } };
    walk(this); return out;
  }
  set textContent(v) { this.childNodes = [new Text(v)]; }
  html() {
    const attrs = [...this._a].map(([k, v]) => ` ${k}="${v}"`).join("");
    const inner = this.childNodes.map((c) => (c instanceof El ? c.html() : c.data)).join("");
    return this.localName === "#root" ? inner : `<${this.localName}${attrs}>${inner}</${this.localName}>`;
  }
}
globalThis.window = {};                                         // no marked: the module only registers a renderer when it exists
globalThis.document = { createElement: (t) => new El(t), addEventListener() {} };

const here = path.dirname(new URL(import.meta.url).pathname);
const { cleanChat, safeMd, agNoImages, agAnswerMd } = await import(pathToFileURL(path.join(here, "..", "frontend", "core", "markdown.mjs")).href);
const clean = (...children) => { const root = new El("#root", {}, children); cleanChat(root); return root.html(); };
let n = 0;
const check = (name, fn) => { fn(); n++; console.log(`   ok ${name}`); };

check("links keep only http(s), mailto and in-page anchors", () => {
  for (const href of ["javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "java\tscript:alert(1)",
                      "data:text/html,<script>alert(1)</script>", "vbscript:x", "file:///etc/passwd", "//evil.example/x", "blob:x"]) {
    assert.equal(clean(new El("a", { href }, ["click"])), "click", href);
  }
  assert.equal(clean(new El("a", { href: "https://ok.example/x" }, ["ok"])),
    '<a href="https://ok.example/x" target="_blank" rel="noopener noreferrer">ok</a>');
  assert.equal(clean(new El("a", { href: "HTTP://ok.example" }, ["ok"])).includes('target="_blank"'), true);
  assert.equal(clean(new El("a", { href: "mailto:a@b.c" }, ["m"])).startsWith('<a href="mailto:a@b.c"'), true);
  assert.equal(clean(new El("a", { href: "#sec" }, ["s"])), '<a href="#sec">s</a>');
  assert.equal(clean(new El("a", {}, ["no href"])), "no href");
});

check("on* event attributes, style, srcset, formaction and xlink:href are removed", () => {
  const out = clean(new El("p", { onclick: "x()", ONERROR: "y()", onMouseOver: "z()", style: "color:red", "xlink:href": "j", id: "keep" }, ["t"]),
                    new El("button", { formaction: "javascript:x" }, ["b"]),
                    new El("img", { src: "/api/x.png", srcset: "https://evil.example/a.png 1x", alt: "a" }));
  assert.equal(out, '<p id="keep">t</p><button>b</button><img src="/api/x.png" alt="a"></img>');
});

check("active and foreign-content elements are dropped with their content", () => {
  for (const tag of ["script", "style", "iframe", "object", "embed", "form", "base", "meta", "link", "svg", "math", "template",
                     "noscript", "frame", "frameset", "applet"]) {
    assert.equal(clean(new El("p", {}, ["a", new El(tag, { src: "x" }, ["inner", new El("b", {}, ["deep"])]), "b"])), "<p>ab</p>", tag);
  }
});

check("images load only from this app's /api/ and /gallery/", () => {
  assert.equal(clean(new El("img", { src: "/gallery/a.png", alt: "g" })), '<img src="/gallery/a.png" alt="g"></img>');
  for (const src of ["https://evil.example/t.png", "//evil.example/t.png", "data:image/png;base64,AA", "http://127.0.0.1:7860/api/x",
                     "javascript:x", "api/x.png", " /etc/x"]) {
    assert.equal(clean(new El("img", { src, alt: "cat" })), "<span>[image: cat]</span>", src);
  }
  assert.equal(clean(new El("img", { src: "https://evil.example/t.png" })), "<span>[image]</span>");
});

check("safeMd: hostile markdown links, images and references lose their target", () => {
  assert.equal(safeMd("[x](javascript:alert(1))"), "x");
  assert.equal(safeMd("[x](&#106;avascript:alert(1))"), "x", "entity-encoded scheme");
  assert.equal(safeMd("[x](&#x6A;avascript:alert(1))"), "x");
  assert.equal(safeMd("[x](data:text/html,hi)"), "x");
  assert.equal(safeMd("[x](<javascript:alert(1)>)"), "x");
  assert.equal(safeMd("[x](https://ok.example/a(b)c)"), "[x](https://ok.example/a(b)c)");
  assert.equal(safeMd("[m](mailto:a@b.c) [h](#top)"), "[m](mailto:a@b.c) [h](#top)");
  assert.equal(safeMd("![cat](https://evil.example/t.png)"), "[image: cat]");
  assert.equal(safeMd("![cat](/api/attachments/x/a.png)"), "![cat](/api/attachments/x/a.png)");
  assert.equal(safeMd("[1]: javascript:alert(1)\n[x][1]"), "[1]: #\n[x][1]");
  assert.equal(safeMd("  [r]: <data:text/html,x>"), "  [r]: #");
  assert.equal(safeMd("[r]: https://ok.example"), "[r]: https://ok.example");
});

check("Agent answers never carry an image", () => {
  assert.equal(agNoImages("![a](/api/x.png)"), "[a](/api/x.png)");
  assert.equal(agNoImages("!![a](/gallery/x.png)"), "[a](/gallery/x.png)");
  assert.equal(agAnswerMd("![a](https://evil.example/x.png)"), "[a](https://evil.example/x.png)");
  assert.equal(agAnswerMd("![a](javascript:x)"), "a");
});

console.log(`   sanitizer checks OK (${n})`);
