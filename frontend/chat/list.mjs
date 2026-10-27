// chat/list.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { savedMessages } from "./record.mjs";
import { createStore } from "../core/store.mjs";
import { $, api, confirmBox, esc, icon, jpost, newId, store, toast } from "../core/util.mjs";
import { showView, syncChatDock, view } from "../ui/views.mjs";
import { activeCodeWs, chDetach, chResume, liveGen, loadCodeView, renderThread, scrollBottom } from "../modules.mjs";

// ---------------- chats
let chats = []; const listState = createStore({ current: null, streaming: null, ctxWindow: 131072 });
listState.codeWorkspace = store.get("code_workspace", null);
listState.chRun = null;          // the task this chat started in Agent mode: { chat, msg, starting, s (run state, see agFresh), el, ui }
const chLive = () => !!(listState.chRun && (listState.chRun.starting || listState.chRun.s.live));

async function loadChats() { try { chats = await api("/api/chats"); } catch { chats = []; } renderHistory(); }
function groupOf(ts) {
  const d = new Date(ts * 1000), now = new Date(), day = 864e5;
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (d >= start) return "Today"; if (d >= start - day) return "Yesterday"; if (d >= start - 7 * day) return "Previous 7 days"; if (d >= start - 30 * day) return "Previous 30 days"; return "Older";
}
function renderHistory() {
  const q = $("search").value.trim().toLowerCase();
  const list = chats.filter((c) => !q || c.title.toLowerCase().includes(q));
  const h = $("history");
  if (!list.length) { h.innerHTML = `<div class="hempty">${q ? "No matching chats" : "Your conversations will appear here."}</div>`; return; }
  let html = "", last = "";
  for (const c of list) {
    const g = groupOf(c.updated);
    if (g !== last) { html += `<div class="hgroup">${g}</div>`; last = g; }
    const on = (view === "chat" || view === "code") && listState.current && listState.current.id === c.id;
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
    loadChats(); return;
  }
  if (act === "rename") {
    const t = item.querySelector(".t"), old = t.textContent;
    t.outerHTML = `<input value="${esc(old)}">`;
    const inp = item.querySelector("input"); inp.focus(); inp.select();
    let finished = false;
    const done = async (save) => { if (finished) return; finished = true; if (save && inp.value.trim() && inp.value !== old) { const c = await api(`/api/chats/${id}`); c.title = inp.value.trim(); await jpost(`/api/chats/${id}`, c, "PUT"); if (listState.current?.id === id) listState.current.title = c.title; if (liveGen?.chat.id === id) liveGen.chat.title = c.title; } loadChats(); };
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
  chDetach(); listState.current = c;
  if (c.workspace) { listState.codeWorkspace = c.workspace; store.set("code_workspace", listState.codeWorkspace); }
  if (view !== "code") showView("chat");
  else { syncChatDock(); renderThread(); loadCodeView(); }
  scrollBottom(true);
  chResume();
}
function newChat() {
  chDetach();
  const ws = view === "code" && activeCodeWs();
  listState.current = ws ? { id: newId(), title: "New coding task", workspace: ws, messages: [] } : null;
  if (view !== "code") showView("chat");
  else syncChatDock();
  renderThread();
}
$("newChat").onclick = () => { chDetach(); listState.current = null; showView("chat"); renderThread(); };
document.addEventListener("keydown", (e) => { if (e.metaKey && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); newChat(); } });
function updateWorkspaceUI() {
  const ws = listState.current?.workspace;
  const pill = $("wsPill");
  if (!pill) return;
  const txt = $("wsTxt");
  const clear = $("wsClear");
  $("modeFoot").hidden = !ws; $("footTxt").hidden = !!ws;
  const compactBtn = $("compactBtn");
  if (compactBtn) compactBtn.style.display = (listState.current?.messages?.length || 0) > 4 ? "" : "none";
  if (ws) {
    pill.classList.add("on");
    const name = ws.split("/").filter(Boolean).pop() || ws;
    if (txt) txt.textContent = name.length > 16 ? name.slice(0, 14) + "…" : name;
    pill.title = `Active workspace: ${ws} (click to change, or × to clear)`;
    if (clear) clear.style.display = "inline";
  } else {
    pill.classList.remove("on");
    if (txt) txt.textContent = "Workspace";
    pill.title = "Set active workspace folder for file & shell tools";
    if (clear) clear.style.display = "none";
  }
}
$("wsPill").onclick = async (e) => {
  if (e.target.id === "wsClear" || e.target.closest("#wsClear")) {
    if (listState.current) {
      delete listState.current.workspace;
      updateWorkspaceUI();
      await saveChat(listState.current);
      toast("Workspace cleared");
    }
    return;
  }
  try {
    const r = await jpost("/api/search/choose-folder");
    if (r && r.path) {
      if (!listState.current) listState.current = { id: newId(), title: "New chat", messages: [] };
      listState.current.workspace = r.path;
      listState.codeWorkspace = r.path;
      store.set("code_workspace", r.path);
      updateWorkspaceUI();
      await saveChat(listState.current);
      if (view === "code") loadCodeView();
      toast(`Workspace: ${r.path}`);
    }
  } catch (err) {
    toast(err.message || "Could not choose folder");
  }
};
async function saveChat(chat = listState.current) {
  if (!chat) return;
  const msgs = savedMessages(chat);
  await jpost(`/api/chats/${chat.id}`, { title: chat.title, messages: msgs, ...(chat.workspace ? { workspace: chat.workspace } : {}),
    ...(chat.workflow_task_id ? { workflow_task_id: chat.workflow_task_id } : {}), ...(chat.workflow_mode ? { workflow_mode: chat.workflow_mode } : {}) }, "PUT");
  loadChats();
}

export {
  chats, chLive, groupOf, listState, loadChats, newChat, openChat, renderHistory, saveChat, updateWorkspaceUI
};
