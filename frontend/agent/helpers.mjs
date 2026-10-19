// agent/helpers.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { esc } from "../core/util.mjs";

// ---------------- agent
const AG_TITLE = "Local Studio", AG_CUA_MIN = "0.28.2", AG_CUA_URL = "https://github.com/trycua/cua";
const AG_EXAMPLES = [
  ["Research laptops", "Research the three best laptops under $1,000 and compare them in a table with prices and links."],
  ["Apple Store hours", "Find the opening hours of the Apple Store on Fifth Avenue in New York."],
  ["Fill a form", "Open https://httpbin.org/forms/post, fill in the form with sample details, and stop before submitting it."],
  ["Add to cart", "Find a USB-C cable on Amazon and add it to my cart. Don't check out."],
];
const AG_TIER = { read: "Read-only", ask: "Ask first", allowed: "Allowed" }, AG_TIER_ORDER = ["read", "ask", "allowed"];
const AG_STATUS = { done: "Finished", stopped: "Stopped", failed: "Failed", interrupted: "Interrupted", running: "Running", waiting: "Waiting for you", paused: "Paused" };

// Pure helpers (also run under plain node by the author's checks).
// An approval title is markdown the server has already escaped: **bold** and \x escapes are all that is honoured, the rest is text.
function agTitleHTML(s) {
  const t = String(s ?? ""); let out = "", bold = false;
  for (let i = 0; i < t.length; i++) {
    const c = t[i];
    if (c === "\\" && i + 1 < t.length) { out += esc(t[++i]); continue; }
    if (c === "*" && t[i + 1] === "*") { out += bold ? "</strong>" : "<strong>"; bold = !bold; i++; continue; }
    out += esc(c);
  }
  return out + (bold ? "</strong>" : "");
}
// The run's pending step: the last approval or question that has no step of the same n after it (and no end).
function agPendingOf(events) {
  let p = null;
  for (const e of events) {
    if (e.type === "approval" || e.type === "question") p = e;
    else if (e.type === "step" && p && e.n === p.n) p = null;
    else if (e.type === "end") p = null;
  }
  return p;
}
function agPausedOf(events) {
  let paused = false;
  for (const e of events) {
    if (e.type === "note" && typeof e.text === "string") { if (e.text.startsWith("Paused while you take over")) paused = true; else if (e.text === "Resumed.") paused = false; }
    else if (e.type === "end") paused = false;
  }
  return paused;
}
const agBoxOf = (b) => Array.isArray(b) && b.length === 4 && b.every((v) => typeof v === "number" && isFinite(v)) && b[2] > 0 && b[3] > 0 ? b : null;
const agWeb = (u) => { try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x : null; } catch { return null; } };
const AG_TAGS = new Set("p br strong em b i del ul ol li a code pre blockquote h1 h2 h3 h4 h5 h6 table thead tbody tr th td hr".split(" "));
const AG_DROP = new Set("script style iframe object embed form input button textarea select svg math link meta base template noscript".split(" "));
function agClean(node) {
  for (const ch of [...node.childNodes]) {
    if (ch.nodeType === 3) continue;
    if (ch.nodeType !== 1) { ch.remove(); continue; }
    const tag = ch.localName;
    if (AG_DROP.has(tag)) { ch.remove(); continue; }
    if (tag === "img") { ch.replaceWith(document.createTextNode(ch.getAttribute("alt") || "")); continue; }   // model output never loads an image
    agClean(ch);
    if (!AG_TAGS.has(tag)) { ch.replaceWith(...ch.childNodes); continue; }
    const href = tag === "a" ? (ch.getAttribute("href") || "").trim() : "";
    for (const a of [...ch.attributes]) ch.removeAttribute(a.name);                // no on* handlers, styles or anything else
    if (tag === "a" && /^(https?:|mailto:)/i.test(href)) { ch.setAttribute("href", href); ch.setAttribute("target", "_blank"); ch.setAttribute("rel", "noopener noreferrer"); }
  }
}
// The final answer is model text: markdown, then only a short list of tags survives.
function agMd(s) {
  const html = window.marked ? marked.parse(String(s || "")) : esc(s).replace(/\n/g, "<br>");
  const t = document.createElement("template"); t.innerHTML = html;
  agClean(t.content);
  const d = document.createElement("div"); d.appendChild(t.content);
  return d.innerHTML;
}
const agShotUrl = (s, n) => `/api/agent/runs/${s.rid}/shots/${encodeURIComponent(n)}`;
// What a run ended with, from its events: the answer markdown (if any), the final status and reason.
function agOutcome(events, fallback = "") {
  let answer = "", status = "", reason = "";
  for (const e of events || []) {
    if (!e || typeof e !== "object") continue;
    if (e.type === "answer") answer = String(e.markdown || "");
    else if (e.type === "end") { status = e.status || ""; reason = e.reason || ""; }
  }
  return { answer, status: status || fallback, reason };
}
function agFinalText(o) {
  if (o.answer.trim()) return o.answer;
  if (o.status === "done") return "The agent finished without giving an answer.";
  return `The agent stopped: ${o.reason || (AG_STATUS[o.status] || o.status || "it was interrupted").toLowerCase()}`;
}
const agWhen = (t) => t ? new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
const agNoteEl = (text) => { const d = document.createElement("div"); d.className = "agnote"; d.textContent = text; return d; };
const agTierKey = (n) => {                       // like the server's _norm_label: no invisible or format characters, then whitespace, case
  const bare = (x) => x.replace(/\p{Cf}/gu, "").replace(/\p{Cc}/gu, " ");
  const k = bare(bare(String(n ?? "")).normalize("NFKC")).replace(/\s+/g, " ").trim().toLowerCase();
  return k.endsWith(".app") ? k.slice(0, -4) : k;
};

export {
  AG_CUA_MIN, AG_CUA_URL, AG_DROP, AG_EXAMPLES, AG_STATUS, AG_TAGS, AG_TIER, AG_TIER_ORDER, AG_TITLE, agBoxOf, agClean,
  agFinalText, agMd, agNoteEl, agOutcome, agPausedOf, agPendingOf, agShotUrl, agTierKey, agTitleHTML, agWeb, agWhen
};
