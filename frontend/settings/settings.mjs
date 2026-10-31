// settings/settings.mjs: moved from index.html's inline script in the refactor's P2 split.
import { $, api, confirmBox, esc, human, jpost, toast } from "../core/util.mjs";
import { listState, loadChats } from "../chat/list.mjs";
import { chDetach } from "../chat/computer.mjs";
import { lib, loadLibrary } from "../library/library.mjs";
import { agRenderModel, loadAgent, loadSearch, refreshContextWindow } from "../modules.mjs";

// ---------------- settings
async function loadManage() { loadStorage(); loadVersions(); loadSearch(); loadAgent(); loadMcp(); loadContext(); loadToolRounds(); loadFeatures(); loadLibrary().then(() => {
  const inst = lib.chat.concat(lib.image).filter((m) => m.installed);
  agRenderModel();
  $("modelsSum").textContent = `${inst.length} downloaded · ${human(inst.reduce((a, m) => a + m.size_bytes, 0))} on disk · ${human(lib.disk_free_bytes)} free`;
}); }
// Maximum context window. The engine keeps the window it started with, so a change applies on the next engine start.
async function loadContext() {
  let r; try { r = await api("/api/context-window"); } catch { return; }
  const sizes = [...new Set([...r.presets, r.value])].sort((a, b) => a - b);
  $("ctxSelect").innerHTML = sizes.map((v) => `<option value="${v}" ${v === r.value ? "selected" : ""}>${v.toLocaleString()} tokens${v === r.default ? " (default)" : ""}</option>`).join("") + `<option value="custom">Custom…</option>`;
  $("ctxCustom").hidden = true; $("ctxCustom").value = r.value;
  const live = r.engine.started_with;
  $("ctxSetSum").textContent = `${r.value.toLocaleString()} tokens` + (r.effective !== r.value ? ` · ${r.effective.toLocaleString()} for the active model` : "") + (live ? (r.restart_required ? ` · the running engine still uses ${live.toLocaleString()}; it restarts with the new size on your next message` : ` · the running engine uses this size`) : " · applies when the engine next starts");
  $("ctxNotes").innerHTML = (r.notes || []).map((n) => `<div class="row"><div class="grow"><div class="sub">${esc(n)}</div></div></div>`).join("");
}
// Tool rounds per reply: how many rounds of tool calls one reply may make (GET/POST /api/tool-rounds); applies from the next message.
async function loadToolRounds() {
  let r; try { r = await api("/api/tool-rounds"); } catch { return; }
  Object.assign($("roundsInput"), { value: r.value, min: r.min, max: r.max });
  $("roundsSum").textContent = `${r.value} rounds per reply${r.value === r.default ? " (default)" : ""} · ${r.min} to ${r.max}`;
}
async function saveToolRounds(value) {
  try { const r = await jpost("/api/tool-rounds", { value }); toast(`Tool rounds per reply set to ${r.value}`); }
  catch (err) { toast(err.message); }
  loadToolRounds();
}
$("roundsSave").onclick = () => saveToolRounds(Number($("roundsInput").value));
$("roundsReset").onclick = () => saveToolRounds(null);
// Voice and web answers: switches for flags in data/settings.json (GET/POST /api/features); each applies without a restart.
async function loadFeatures() {
  let r; try { r = await api("/api/features"); } catch { return; }
  const on = Object.fromEntries(r.features.map((f) => [f.name, f.on]));
  document.querySelectorAll("#featSet [data-feature]").forEach((sw) => { sw.checked = !!on[sw.dataset.feature]; });
  $("featDecide").disabled = !on["voice.enabled"];                       // the check runs beside Voice
  $("featSum").textContent = [on["voice.enabled"] ? (on["voice.decide"] ? "Voice on, with the live-facts check" : "Voice on") : "Voice off",
    on["harness.web_fast"] ? "faster web answers on" : "faster web answers off"].join(" · ");
  $("micBtn").hidden = $("navVoice").hidden = !on["voice.enabled"];      // the mic button and the Voice view follow the switch
}
document.querySelectorAll("#featSet [data-feature]").forEach((sw) => {
  sw.onchange = async () => {
    try { await jpost("/api/features", { name: sw.dataset.feature, on: sw.checked }); toast(`${sw.closest(".row").querySelector(".ttl").textContent} ${sw.checked ? "on" : "off"}`); }
    catch (err) { toast(err.message); }
    loadFeatures();
  };
});
$("ctxSelect").onchange = () => { $("ctxCustom").hidden = $("ctxSelect").value !== "custom"; };
async function saveContext(value) {
  try { const r = await jpost("/api/context-window", { value }); toast(`Context window set to ${r.value.toLocaleString()} tokens`); await loadContext(); refreshContextWindow(); }
  catch (err) { toast(err.message); }
}
$("ctxSave").onclick = () => saveContext($("ctxSelect").value === "custom" ? Number($("ctxCustom").value) : Number($("ctxSelect").value));
$("ctxReset").onclick = () => saveContext(null);
let mcpServers = [];
async function loadMcp() {
  let r; try { r = await api("/api/mcp/servers"); } catch { return; }
  mcpServers = r.servers || [];
  $("mcpSetSum").textContent = mcpServers.length ? `${mcpServers.length} configured server${mcpServers.length === 1 ? "" : "s"}` : "Custom Model Context Protocol stdio servers available in chat";
  $("mcpList").innerHTML = mcpServers.map((s) => `<div class="row" data-mcp="${esc(s.id)}">
    <div class="grow"><div class="ttl">${esc(s.id)} ${s.alive ? `<span class="badge ok" style="margin-left:6px">Connected</span>` : ""}</div>
    <div class="sub mono">${esc([s.command, ...(s.args || [])].join(" "))}</div></div>
    <button class="btn sm" data-mcptest="${esc(s.id)}">Test</button>
    <button class="btn sm danger" data-mcpdel="${esc(s.id)}">Remove</button>
  </div>`).join("");
}
$("mcpAdd").onclick = async () => {
  const id = $("mcpId").value.trim(), command = $("mcpCmd").value.trim(), argsRaw = $("mcpArgs").value.trim();
  if (!id || !command) return toast("Enter both a server ID and command");
  const args = argsRaw ? argsRaw.split(/\s+/).filter(Boolean) : [];
  try {
    await jpost("/api/mcp/servers", { id, command, args, enabled: true });
    $("mcpId").value = ""; $("mcpCmd").value = ""; $("mcpArgs").value = "";
    toast(`MCP server ${id} saved`);
    loadMcp();
  } catch (err) { toast(err.message); }
};
$("mcpList").onclick = async (e) => {
  const tb = e.target.closest("[data-mcptest]");
  if (tb) {
    const id = tb.dataset.mcptest;
    const server = mcpServers.find((s) => s.id === id);
    if (!server || !await confirmBox(`Start MCP server ${id}?`, server.launch_preview || [server.command, ...(server.args || [])].join(" "), "Start server")) return;
    tb.disabled = true; tb.textContent = "Testing…";
    try {
      const r = await jpost(`/api/mcp/servers/${encodeURIComponent(id)}/test`, { approve_launch: server.launch_fingerprint });
      toast(`${id}: ${r.tools_count || 0} tool(s) discovered`);
      loadMcp();
    } catch (err) { toast(err.message); tb.disabled = false; tb.textContent = "Test"; }
    return;
  }
  const db = e.target.closest("[data-mcpdel]");
  if (db) {
    const id = db.dataset.mcpdel;
    try {
      await api(`/api/mcp/servers/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast(`Removed ${id}`);
      loadMcp();
    } catch (err) { toast(err.message); }
  }
};
$("apiCopy").onclick = () => { navigator.clipboard.writeText($("apiUrl").textContent); toast("Copied"); };
async function loadVersions() {
  let v; try { v = await api("/api/versions"); } catch { return; }
  const rows = [["Local Studio", v.build ? `${v.app} (build ${v.build})` : `${v.app} (source)`], ["mlx-dspark", v["mlx-dspark"]], ["mlx-vlm", v["mlx-vlm"]], ["MLX", v.mlx], ["mflux", v.mflux], ["llama.cpp", v["llama.cpp"] || "not installed"]];
  $("verKvs").innerHTML = rows.map(([l, val]) => `<div><div class="l">${l}</div><div class="v">${esc(val || "–")}</div></div>`).join("");
  const u = v.update || {};
  $("updRow").style.display = u.status && u.status !== "idle" ? "" : "none";
  $("updBtn").disabled = u.status === "running";
  if (u.status === "running") { $("updStatus").textContent = `Updating… ${u.step || ""}`; setTimeout(loadVersions, 3000); }
  if (u.status === "done") { const ch = Object.entries(u.changed || {}); $("updStatus").textContent = ch.length ? "Updated: " + ch.map(([k, [a, b]]) => `${k} ${a || "–"} → ${b}`).join(", ") + ". Restart the engine to use the new versions." : "Everything is already up to date."; }
}
$("updBtn").onclick = async () => { try { await jpost("/api/versions/update"); toast("Checking for updates…"); loadVersions(); } catch (e) { toast(e.message); } };
const STORAGE = [["gallery", "Generated images", "Images in your library and their settings"], ["chats", "Conversations", "Saved chat history"], ["uploads", "Uploads", "Reference images you uploaded"], ["logs", "Logs", "Server and download logs"], ["engine_cache", "Engine calibration", "mlx-dspark speed calibration, rebuilt automatically"]];
async function loadStorage() {
  let s; try { s = await api("/api/storage"); } catch { return; }
  $("diskFree").textContent = `${human(s.disk_free_bytes)} free on this Mac`; $("unPath").textContent = `(${s.root})`;
  $("storageRows").innerHTML = STORAGE.map(([k, t, d]) => {
    const it = s[k] || {}; const cnt = it.count != null ? ` · ${it.count} item${it.count === 1 ? "" : "s"}` : "";
    return `<div class="row"><div class="grow"><div class="ttl">${t}</div><div class="sub">${d}${cnt}</div></div><div class="size">${human(it.bytes || 0)}</div><button class="btn sm" data-clear="${k}" ${it.bytes ? "" : "disabled"}>Clear</button></div>`;
  }).join("");
  $("storageRows").onclick = async (e) => {
    const b = e.target.closest("[data-clear]"); if (!b) return;
    const [, t] = STORAGE.find(([k]) => k === b.dataset.clear);
    if (!(await confirmBox(`Clear ${t.toLowerCase()}?`, "This permanently deletes the files and can't be undone.", "Clear"))) return;
    try { await jpost("/api/storage/clear", { what: b.dataset.clear }); toast(`${t} cleared`); } catch (err) { toast(err.message); }
    if (b.dataset.clear === "chats") { chDetach(); listState.current = null; listState.sessions = { chat: null, code: null, voice: null }; loadChats(); }
    loadStorage();
  };
}
$("unConfirm").oninput = () => $("unBtn").disabled = $("unConfirm").value.trim().toLowerCase() !== "uninstall";
$("unBtn").onclick = async () => {
  const removeApp = $("unApp").checked;
  if (!(await confirmBox("Uninstall Local Studio?", `This removes the engines, the Mac app and every downloaded model${removeApp ? ", and deletes the app folder with your images and chats" : ""}. This page will stop working.`, "Uninstall"))) return;
  try { const r = await jpost("/api/uninstall", { remove_app: removeApp }); document.body.innerHTML = `<div style="height:100vh;display:grid;place-items:center;font:15px Inter,system-ui;color:#555;text-align:center;padding:24px"><div><h2 style="color:#111;font-weight:600">Uninstalling…</h2><p>Progress is written to <code>${esc(r.log)}</code>.<br>You can close this tab.</p></div></div>`; }
  catch (e) { toast(e.message); }
};

export {
  loadManage, loadMcp, loadStorage, loadVersions, mcpServers, STORAGE
};
