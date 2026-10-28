// core/markdown.mjs: moved from index.html's inline script in the refactor's P2 split.
import { esc, icon, toast } from "./util.mjs";

if (window.marked) marked.use({ renderer: { html(h) { return esc(typeof h === "string" ? h : h.text); } } });
// Model and page text reaches this renderer (and saved chats re-render through it), so what it keeps is an allowlist:
// links only to http(s), mailto and in-page anchors; images only from this app's own /api/ and /gallery/; no active elements.
const CHAT_DROP = new Set("script style iframe object embed form base meta link svg math template noscript frame frameset applet".split(" "));
function cleanChat(root) {
  for (const el of [...root.querySelectorAll("*")]) {
    const tag = el.localName;
    if (CHAT_DROP.has(tag)) { el.remove(); continue; }
    for (const a of [...el.attributes]) if (/^on/i.test(a.name) || ["style", "srcset", "formaction", "xlink:href"].includes(a.name.toLowerCase())) el.removeAttribute(a.name);
    if (tag === "a") {
      const href = (el.getAttribute("href") || "").trim();
      if (!/^(https?:|mailto:|#)/i.test(href)) el.replaceWith(...el.childNodes);
      else if (!href.startsWith("#")) { el.setAttribute("target", "_blank"); el.setAttribute("rel", "noopener noreferrer"); }
    } else if (tag === "img" && !/^\/(api|gallery)\//.test((el.getAttribute("src") || "").trim())) {
      const alt = el.getAttribute("alt"), sp = document.createElement("span");
      sp.textContent = alt ? `[image: ${alt}]` : "[image]"; el.replaceWith(sp);
    }
  }
}
// The same rule on markdown text, so a stored chat does not carry a hostile link or image from the start.
function safeMd(s) {
  const first = (u) => u.trim().replace(/^<|>$/g, "").split(/\s+/)[0], url = "\\(((?:[^()]|\\([^()]*\\))*)\\)";   // one level of ( ) inside the address
  return String(s || "")
    .replace(new RegExp("!\\[([^\\]]*)\\]" + url, "g"), (m, alt, u) => /^\/(api|gallery)\//.test(first(u)) ? m : `[image: ${alt}]`)
    .replace(new RegExp("(?<!!)\\[([^\\]]*)\\]" + url, "g"), (m, t, u) => /^(https?:|mailto:|#)/i.test(first(u)) ? m : t)
    .replace(/^(\s{0,3}\[[^\]]+\]:\s*)(\S+)/gm, (m, a, u) => /^(https?:|mailto:|#)/i.test(u.replace(/^</, "")) ? m : a + "#");
}
// Answers from the Agent are page-steered text: they never carry an image into a chat (a same-origin one would load on every open).
function agNoImages(s) {                                     // a "!" before "[" is what makes an image: without it every form is a link or plain text, in one pass and again on any later one
  return String(s || "").replace(/!+(?=\[)/g, "");
}
const agAnswerMd = (s) => safeMd(agNoImages(s));
function md(s) {
  const html = window.marked ? marked.parse(s || "") : esc(s).replace(/\n/g, "<br>");
  const t = document.createElement("template"); t.innerHTML = html; cleanChat(t.content);   // a template is inert: nothing loads or runs while it is cleaned
  const tmp = document.createElement("div"); tmp.appendChild(t.content);
  tmp.querySelectorAll("pre > code").forEach((code) => {
    const lang = (code.className.match(/language-([\w+#-]+)/) || [])[1] || "code";
    const wrap = document.createElement("div"); wrap.className = "codeblock";
    wrap.innerHTML = `<div class="ch"><span>${esc(lang)}</span><button data-copycode>${icon("i-copy", "width:14px;height:14px")}Copy</button></div>`;
    const pre = code.parentElement; pre.replaceWith(wrap); wrap.appendChild(pre);
  });
  return tmp.innerHTML;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copycode]");
  if (b) { navigator.clipboard.writeText(b.closest(".codeblock").querySelector("code").innerText); b.innerHTML = `${icon("i-check", "width:14px;height:14px")}Copied`; setTimeout(() => b.innerHTML = `${icon("i-copy", "width:14px;height:14px")}Copy`, 1500); }
  const c = e.target.closest("[data-copy]");
  if (c) { navigator.clipboard.writeText(c.dataset.copy); toast("Copied"); }
});

export {
  agAnswerMd, agNoImages, CHAT_DROP, cleanChat, md, safeMd
};
