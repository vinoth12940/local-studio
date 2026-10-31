// chat/list.mjs: moved from index.html's inline script in the refactor's P2 split.
import { savedMessages } from "./record.mjs";
import { createStore } from "../core/store.mjs";
import { $, api, confirmBox, esc, icon, jpost, newId, store, toast } from "../core/util.mjs";
import { showView, syncChatDock, view } from "../ui/views.mjs";
import { activeCodeWs, chDetach, chResume, liveGen, loadCodeView, renderThread, scrollBottom, voiceNewChat, voiceOpenChat } from "../modules.mjs";

// ---------------- chats
// Chat, Code and Voice each keep their own conversations. `current` is the one the open view shows; `sessions` keeps each
// kind's open conversation while its view is not shown. A workspace belongs to Code conversations only (chatKind).
let chats = []; const listState = createStore({ current: null, streaming: null, ctxWindow: 131072 });
listState.codeWorkspace = store.get("code_workspace", null);
listState.chRun = null;          // the task this chat started in Agent mode: { chat, msg, starting, s (run state, see agFresh), el, ui }
listState.sessions = { chat: null, code: null, voice: null };
const chLive = () => !!(listState.chRun && (listState.chRun.starting || listState.chRun.s.live));
const kindOfView = (v) => (v === "code" || v === "voice" ? v : "chat");                  // Images, Library, Search…: Chat's
const chatKind = (c) => (c?.kind === "chat" || c?.kind === "voice" ? c.kind : c?.kind === "code" || c?.workspace ? "code" : "chat");
const sessionOf = (kind) => (kindOfView(view) === kind ? listState.current : listState.sessions[kind]);
// Called by showView before the view changes: the conversation on screen goes back to its kind, and the new view's own
// conversation comes back on screen. True when it changed.
function switchSession(from, to) {
  const a = kindOfView(from), b = kindOfView(to);
  if (a === b) return false;
  const cur = listState.current;
  if (cur) listState.sessions[chatKind(cur)] = cur; else listState.sessions[a] = null;
  chDetach();
  listState.current = listState.sessions[b];
  return true;
}
function forgetSession(id) {
  for (const k of Object.keys(listState.sessions)) if (listState.sessions[k]?.id === id) listState.sessions[k] = null;
}
const HISTORY_WORDS = {
  chat: ["Search chats", "Your conversations will appear here."],
  code: ["Search coding sessions", "Coding sessions in this folder will appear here."],
  voice: ["Search voice conversations", "Voice conversations will appear here."],
};

async function loadChats() { try { chats = await api("/api/chats"); } catch { chats = []; } renderHistory(); }
function groupOf(ts) {
  const d = new Date(ts * 1000), now = new Date(), day = 864e5;
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (d >= start) return "Today"; if (d >= start - day) return "Yesterday"; if (d >= start - 7 * day) return "Previous 7 days"; if (d >= start - 30 * day) return "Previous 30 days"; return "Older";
}
function renderHistory() {                               // the open view's conversations only (Code: this folder's)
  const kind = kindOfView(view), ws = kind === "code" ? activeCodeWs() : "";
  const q = $("search").value.trim().toLowerCase();
  const list = chats.filter((c) => chatKind(c) === kind && (kind !== "code" || c.workspace === ws) && (!q || c.title.toLowerCase().includes(q)));
  const h = $("history");
  $("search").placeholder = HISTORY_WORDS[kind][0];
  if (!list.length) { h.innerHTML = `<div class="hempty">${q ? "No matching chats" : HISTORY_WORDS[kind][1]}</div>`; return; }
  let html = "", last = "";
  for (const c of list) {
    const g = groupOf(c.updated);
    if (g !== last) { html += `<div class="hgroup">${g}</div>`; last = g; }
    const on = listState.current && listState.current.id === c.id;
    const live = liveGen && liveGen.chat.id === c.id ? `<span class="hlive" title="Answering…"></span>` : "";
    html += `<div class="hitem${on ? " on" : ""}" data-id="${c.id}">${live}<span class="t">${esc(c.title)}</span><span class="acts"><button data-act="rename" title="Rename">${icon("i-pen")}</button><button data-act="delete" title="Delete">${icon("i-trash")}</button></span></div>`;
  }
  h.innerHTML = html;
}
$("search").oninput = renderHistory;
$("history").onclick = async (e) => {
  const item = e.target.closest(".hitem"); if (!item) return;
  const id = item.dataset.id, act = e.target.closest("button")?.dataset.act;
  if (act === "delete") {
    const c = chats.find((x) => x.id === id);
    if (!(await confirmBox("Delete chat?", `“${c?.title}” will be permanently deleted.`))) return;
    if (liveGen?.chat.id === id) { liveGen.chat.deleted = true; listState.streaming?.abort(); }    // its reply must not save the chat again
    await api(`/api/chats/${id}`, { method: "DELETE" });
    if (listState.current?.id === id) newChat();
    forgetSession(id);                                                                    // or a later save would bring it back
    loadChats(); return;
  }
  if (act === "rename") {
    const t = item.querySelector(".t"), old = t.textContent;
    t.outerHTML = `<input value="${esc(old)}">`;
    const inp = item.querySelector("input"); inp.focus(); inp.select();
    let finished = false;
    const done = async (save) => { if (finished) return; finished = true; if (save && inp.value.trim() && inp.value !== old) { const c = await api(`/api/chats/${id}`); c.title = inp.value.trim(); await jpost(`/api/chats/${id}`, c, "PUT"); for (const x of [listState.current, ...Object.values(listState.sessions), liveGen?.chat]) if (x?.id === id) x.title = c.title; } loadChats(); };
    inp.onkeydown = (ev) => { if (ev.key === "Enter") done(true); if (ev.key === "Escape") done(false); };
    inp.onblur = () => done(true); return;
  }
  if (e.target.tagName === "INPUT") return;
  await openChat(id);
};
async function openChat(id) {
  let c;
  if (liveGen && liveGen.chat.id === id) c = liveGen.chat;                               // still answering: the copy in memory is the live one
  else { try { c = await api(`/api/chats/${id}`); } catch { return toast("Could not open chat"); } }
  const kind = chatKind(c);                                                              // it opens in its own view
  if (kindOfView(view) === kind) { chDetach(); listState.current = c; } else listState.sessions[kind] = c;
  if (kind === "code") { listState.codeWorkspace = c.workspace; store.set("code_workspace", listState.codeWorkspace); }
  if (kind === "voice") { if (view !== "voice") showView("voice"); else voiceOpenChat(); return; }
  if (kind === "chat" || view !== "code") showView(kind);
  else { syncChatDock(); renderThread(); loadCodeView(); }
  scrollBottom(true);
  chResume();
}
function newChat() {                                     // a new conversation of the open view's kind
  chDetach();
  const kind = kindOfView(view), ws = kind === "code" && activeCodeWs();
  if (kind === "voice") return voiceNewChat();           // also stops a reply that is still running and clears the display
  listState.current = ws ? { id: newId(), title: "New coding task", kind, workspace: ws, messages: [] } : null;
  if (kind === "chat") showView("chat");
  else syncChatDock();
  renderThread();
}
$("newChat").onclick = () => newChat();
document.addEventListener("keydown", (e) => { if (e.metaKey && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); newChat(); } });
function updateWorkspaceUI() {
  const ws = listState.current?.workspace;
  $("modeFoot").hidden = !ws; $("footTxt").hidden = !!ws;
  const compactBtn = $("compactBtn");
  if (compactBtn) compactBtn.style.display = (listState.current?.messages?.length || 0) > 4 ? "" : "none";
}
async function saveChat(chat = listState.current) {
  if (!chat) return;
  const msgs = savedMessages(chat), kind = chatKind(chat);
  await jpost(`/api/chats/${chat.id}`, { title: chat.title, kind, messages: msgs, ...(kind === "code" && chat.workspace ? { workspace: chat.workspace } : {}),
    ...(chat.workflow_task_id ? { workflow_task_id: chat.workflow_task_id } : {}), ...(chat.workflow_mode ? { workflow_mode: chat.workflow_mode } : {}) }, "PUT");
  loadChats();
}

export {
  chatKind, chats, chLive, forgetSession, groupOf, kindOfView, listState, loadChats, newChat, openChat, renderHistory, saveChat,
  sessionOf, switchSession, updateWorkspaceUI
};
