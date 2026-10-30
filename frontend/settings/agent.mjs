// settings/agent.mjs: moved from index.html's inline script in the refactor's P2 split.
import { $, confirmBox, esc, jpost, toast } from "../core/util.mjs";
import { AG_CUA_MIN, AG_CUA_URL, AG_TIER, AG_TIER_ORDER, agTierKey } from "../agent/helpers.mjs";
import { ag, agPermMissing } from "../agent/state.mjs";
import { loadAgent } from "../agent/agent.mjs";
import { lib } from "../library/library.mjs";

// ---- settings
function agRenderSettings() {
  const st = ag.st; if (!st) return;
  const c = st.cua || {}, s = st.settings || {}, perm = agPermMissing(st), ready = c.installed && c.ok_version && !perm;
  $("agEnable").checked = !!st.enabled;
  $("agSetSum").textContent = !st.enabled ? "Let chat take actions in your browser and Mac apps. You can also enable access when a chat task needs it." : ready ? "Chat can take actions when you ask, with approval before risky steps." : "Computer access is on, but CuaDriver needs attention.";
  $("agSetBody").style.display = st.enabled ? "" : "none";
  if (document.activeElement !== $("agSteps")) $("agSteps").value = s.max_steps ?? "";
  if (document.activeElement !== $("agMins")) $("agMins").value = s.max_minutes ?? "";
  agRenderModel();
  const sig = JSON.stringify([st.enabled, c, s, st.permissions]);
  if (sig === ag.setSig) return;
  ag.setSig = sig;
  const link = `<a href="${AG_CUA_URL}" target="_blank" rel="noopener noreferrer">github.com/trycua/cua</a>`;
  let msg, act = "";
  if (!c.installed) msg = `Not installed. Get it from ${link}.`;
  else if (!c.ok_version) msg = `Update needed (have ${esc(c.version || "unknown")}, need ${AG_CUA_MIN}). Update it from ${link}.`;
  else if (perm) { msg = `v${esc(c.version)}, but macOS hasn't given it Accessibility and Screen Recording yet. No task can start until it has them.`; act = `<button class="btn sm" data-agperm>Grant permissions</button>`; }
  else { msg = `Ready v${esc(c.version)}. macOS asks once for Accessibility and Screen Recording.`; act = `<button class="btn sm" data-agperm>Grant permissions</button>`; }
  $("agCua").innerHTML = `<div class="grow"><div class="ttl">CuaDriver</div><div class="sub">${msg}</div></div>${act}`;
  const tel = $("agTelemetry"); tel.style.display = c.installed ? "" : "none";
  tel.innerHTML = `<div class="grow"><div class="ttl">Telemetry</div><div class="sub">${c.telemetry === true ? "On. CuaDriver may send usage data off this Mac." : c.telemetry === false ? "Off." : "Unknown."}</div></div>${c.telemetry === true ? `<button class="btn sm" data-agtele>Turn off</button>` : ""}`;
  agRenderTiers();
}
// Model for computer tasks: the downloaded chat models (the library may load after the status; settings.mjs calls this again).
export function agRenderModel() {
  const sel = $("agModel"), chosen = ag.st?.settings?.model || "";
  if (!sel || document.activeElement === sel) return;
  const models = (lib.chat || []).filter((m) => m.installed);
  sel.innerHTML = `<option value="">Same as the chat model</option>` + models.map((m) =>
    `<option value="${esc(m.id)}"${m.id === chosen ? " selected" : ""}>${esc(m.name)}${m.vision ? " · reads screenshots" : ""}</option>`).join("");
  sel.value = models.some((m) => m.id === chosen) ? chosen : "";
}
$("agModel").onchange = () => agSave({ model: $("agModel").value || null });
function agTierSelect(name, tier) {
  return `<div class="sel sm"><select data-agtier="${esc(name)}" title="${esc(name)}">${Object.entries(AG_TIER).map(([v, l]) => `<option value="${v}"${v === tier ? " selected" : ""}>${l}</option>`).join("")}</select></div>`;
}
function agRenderTiers() {
  const st = ag.st, defs = st.tier_defaults || {}, ov = st.settings?.tiers || {};
  const defByKey = new Map(Object.entries(defs).map(([n, t]) => [agTierKey(n), t]));
  const mineKeys = new Set(Object.keys(ov).map(agTierKey));
  const mine = Object.entries(ov).sort((a, b) => a[0].localeCompare(b[0]));
  const rest = Object.entries(defs).filter(([n]) => !mineKeys.has(agTierKey(n))).sort((a, b) => AG_TIER_ORDER.indexOf(a[1]) - AG_TIER_ORDER.indexOf(b[1]) || a[0].localeCompare(b[0]));
  const open = $("agTierList").querySelector("#agTierDefaults")?.open;
  $("agTierList").innerHTML = (mine.length ? `<div class="agtierh">Your changes</div>` + mine.map(([n, t]) => `<div class="agtier"><span class="nm" title="${esc(n)}">${esc(n)}</span>${agTierSelect(n, t)}<button class="btn sm" data-agtierrm="${esc(n)}">${defByKey.has(agTierKey(n)) ? "Use default" : "Remove"}</button></div>`).join("") : "")
    + `<details id="agTierDefaults"${open ? " open" : ""}><summary>Built-in apps (${rest.length})</summary>${rest.map(([n, t]) => `<div class="agtier"><span class="nm" title="${esc(n)}">${esc(n)}</span>${agTierSelect(n, t)}</div>`).join("")}</details>`;
}
async function agSave(patch) {
  try { await jpost("/api/agent/settings", patch, "PUT"); } catch (e) { toast(e.message); }
  ag.setSig = ""; loadAgent();
}
function agSetTier(name, tier) {                          // PUT replaces the whole map; a default app set back to its default needs no entry
  const ov = { ...(ag.st.settings.tiers || {}) }, key = agTierKey(name);
  for (const k of Object.keys(ov)) if (agTierKey(k) === key) delete ov[k];
  const def = Object.entries(ag.st.tier_defaults || {}).find(([n]) => agTierKey(n) === key);
  if (!(def && def[1] === tier)) ov[name.trim()] = tier;
  return agSave({ tiers: ov });
}
$("agTierList").onchange = (e) => { const s = e.target.closest("[data-agtier]"); if (s) agSetTier(s.dataset.agtier, s.value); };
$("agTierList").onclick = (e) => {
  const b = e.target.closest("[data-agtierrm]"); if (!b) return;
  const ov = { ...(ag.st.settings.tiers || {}) }, key = agTierKey(b.dataset.agtierrm);
  for (const k of Object.keys(ov)) if (agTierKey(k) === key) delete ov[k];
  agSave({ tiers: ov });
};
$("agTierAdd").onclick = async () => {
  const name = $("agTierName").value.trim().slice(0, 80); if (!name || !ag.st) return $("agTierName").focus();
  await agSetTier(name, $("agTierNew").value); $("agTierName").value = "";
};
$("agTierName").addEventListener("keydown", (e) => { if (e.key === "Enter") $("agTierAdd").click(); });
function agLimit(el, key, lo, hi, label) {
  el.onchange = () => {
    let v = parseInt(el.value, 10);
    if (!isFinite(v)) v = ag.st?.settings?.[key] ?? lo;
    const c = Math.max(lo, Math.min(hi, v));
    if (c !== v) toast(`${label} must be ${lo} to ${hi}`);
    el.value = c; agSave({ [key]: c });
  };
}
agLimit($("agSteps"), "max_steps", 1, 200, "Steps"); agLimit($("agMins"), "max_minutes", 1, 120, "Minutes");
$("agEnable").onchange = async () => {
  try { await jpost("/api/agent/enable", { on: $("agEnable").checked }); } catch (e) { toast(e.message); }
  ag.setSig = ""; loadAgent();
};
async function agGrant() {
  try { await jpost("/api/agent/permissions"); toast("Follow the macOS prompts for Accessibility and Screen Recording"); } catch (err) { toast(err.message); }
}
$("agCua").onclick = (e) => { if (e.target.closest("[data-agperm]")) agGrant(); };
$("agTelemetry").onclick = async (e) => {
  if (!e.target.closest("[data-agtele]")) return;
  if (!(await confirmBox("Turn off CuaDriver telemetry?", "This runs cua-driver telemetry disable, so CuaDriver stops sending usage data. It changes a CuaDriver setting, not a Local Studio one.", "Turn off"))) return;
  try { await jpost("/api/agent/telemetry/disable"); toast("Telemetry turned off"); } catch (err) { toast(err.message); }
  ag.setSig = ""; loadAgent();
};
$("agReset").onclick = async () => {
  if (!(await confirmBox("Reset the browser profile?", "The agent's browser forgets every site it signed in to and deletes its cookies. Your own browser is not touched.", "Reset"))) return;
  try { await jpost("/api/agent/profile/reset"); toast("Browser profile reset"); } catch (e) { toast(e.message); }
};

export {
  agGrant, agLimit, agRenderSettings, agRenderTiers, agSave, agSetTier, agTierSelect
};
