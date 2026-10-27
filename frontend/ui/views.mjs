// ui/views.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { $, DEFAULT_MAX_TOKENS, store, toast } from "../core/util.mjs";
import { setCollapsed } from "./theme.mjs";
import {
  agFetchStatus, agOpen, codeTermS, codeTermStart, codeTermTheme, loadCodeView, loadGallery, loadLibrary, loadManage,
  loadSearch, renderHistory, renderThread, scrollBottom, updateHeader, voiceViewShown
} from "../modules.mjs";

// ---------------- views
let view = "chat";
function chatHost() {
  return view === "code" && $("codeChatDock") ? $("codeChatDock") : $("v-chat");
}
function syncChatDock() {
  const host = chatHost();
  if (!host) return;
  const scroll = $("chatScroll"), jump = $("jumpBtn"), wrap = $("composerWrap");
  if (scroll && scroll.parentElement !== host) host.appendChild(scroll);
  if (jump && jump.parentElement !== host) host.appendChild(jump);
  const w = document.querySelector(".welcome");
  if (w && w.parentElement !== host) host.prepend(w);
  if (wrap && (!w || !w.contains(wrap)) && wrap.parentElement !== host) host.appendChild(wrap);
}
function showView(v) {
  view = v;
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("on", s.id === "v-" + v));
  document.querySelectorAll(".navitem[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  updateHeader();
  $("modelBtn").querySelector("svg").style.display = (v === "chat" || v === "code") ? "" : "none";
  closeMenus();
  if (v === "code" || v === "chat") {
    syncChatDock();
    renderThread();
    scrollBottom(true);                                                                 // the thread moved between views: show the latest turn
  }
  if (v === "code") {
    loadCodeView(); setTimeout(() => $("input").focus(), 0);
    codeTermStart().then(() => { if (codeTermS) codeTermS.term.options.theme = codeTermTheme(); });
  }
  if (v === "images") { loadGallery(); loadLibrary(); }
  if (v === "library") loadLibrary();
  if (v === "manage") loadManage();
  if (v === "search") { loadSearch(); setTimeout(() => $("srQuery").focus(), 0); }
  if (v === "agent") agOpen();
  voiceViewShown(v);
  if (v === "chat") { setTimeout(() => $("input").focus(), 0); agFetchStatus(); }
  renderHistory();
  if (innerWidth < 820) setCollapsed(true);
}
document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
function closeMenus() { $("modelMenu").classList.remove("on"); }
$("modelBtn").onclick = (e) => { e.stopPropagation(); if (view !== "chat" && view !== "code") return; $("modelMenu").classList.toggle("on"); };
document.addEventListener("click", (e) => { if (!e.target.closest("#modelMenu") && !e.target.closest("#modelBtn")) closeMenus(); if (!e.target.closest("#inspector") && !e.target.closest("#inspBtn")) $("inspector").classList.remove("on"); });
$("inspBtn").onclick = (e) => { e.stopPropagation(); $("inspector").classList.toggle("on"); };
$("inspClose").onclick = () => $("inspector").classList.remove("on");
$("mmSettings").onclick = () => { closeMenus(); $("settingsDlg").showModal(); };
$("settingsDone").onclick = () => { $("settingsDlg").close(); store.set("settings", { sys: $("sysPrompt").value, max: $("maxTok").value, temp: $("temp").value }); toast("Settings saved"); };
{ const s = store.get("settings", {}); $("sysPrompt").value = s.sys || ""; $("maxTok").value = s.max || DEFAULT_MAX_TOKENS; $("temp").value = s.temp || ""; }

export {
  chatHost, closeMenus, showView, syncChatDock, view
};
