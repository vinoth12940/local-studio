// settings/settings.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { $, api, confirmBox, esc, human, jpost, toast } from "../core/util.mjs";
import { listState, loadChats } from "../chat/list.mjs";
import { chDetach } from "../chat/computer.mjs";
import { lib, loadLibrary } from "../library/library.mjs";
import { loadAgent, loadSearch } from "../modules.mjs";

// ---------------- settings
async function loadManage() { loadStorage(); loadVersions(); loadSearch(); loadAgent(); loadMcp(); loadLibrary().then(() => {
  const inst = lib.chat.concat(lib.image).filter((m) => m.installed);
  $("modelsSum").textContent = `${inst.length} downloaded · ${human(inst.reduce((a, m) => a + m.size_bytes, 0))} on disk · ${human(lib.disk_free_bytes)} free`;
}); }
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
    tb.disabled = true; tb.textContent = "Testing…";
    try {
      const r = await jpost(`/api/mcp/servers/${encodeURIComponent(id)}/test`);
      toast(`${id}: ${r.count || 0} tool(s) discovered`);
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
  const rows = [["Local Studio", v.app], ["mlx-dspark", v["mlx-dspark"]], ["mlx-vlm", v["mlx-vlm"]], ["MLX", v.mlx], ["mflux", v.mflux], ["llama.cpp", v["llama.cpp"] || "not installed"]];
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
    if (b.dataset.clear === "chats") { chDetach(); listState.current = null; loadChats(); }
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
