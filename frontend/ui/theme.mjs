// ui/theme.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { $, store } from "../core/util.mjs";
import { codeTermS, codeTermTheme, showView } from "../modules.mjs";

// ---------------- theme & layout
function applyTheme(t) { if (t) document.documentElement.dataset.theme = t; const dark = (t || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark"; $("themeIcon").setAttribute("href", dark ? "#i-sun" : "#i-moon"); try { if (codeTermS) codeTermS.term.options.theme = codeTermTheme(); } catch {} }                       // codeTermS is declared later; at start-up there is no terminal yet
applyTheme(store.get("theme", null));
$("themeBtn").onclick = (e) => { e.stopPropagation(); const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); const n = cur === "dark" ? "light" : "dark"; store.set("theme", n); applyTheme(n); };
function setCollapsed(c) { $("app").classList.toggle("collapsed", c); $("expand").style.display = c ? "" : "none"; store.set("collapsed", c); }
setCollapsed(store.get("collapsed", innerWidth < 820));
$("collapse").onclick = () => setCollapsed(true); $("expand").onclick = () => setCollapsed(false); $("scrim").onclick = () => setCollapsed(true);
$("engineCard").onclick = () => showView("manage");

export {
  applyTheme, setCollapsed
};
