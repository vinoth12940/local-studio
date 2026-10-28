// ui/engine.mjs: moved from index.html's inline script in the refactor's P2 split.
import { $, api, gib, jpost, toast } from "../core/util.mjs";
import { closeMenus } from "./views.mjs";
import { activeChat, libraryState, listState, updateHeader, updateSend } from "../modules.mjs";

// ---------------- engine status
let health = null, metrics = null, engineState = "down";
// The window the next reply is budgeted against while no engine reports one: the setting clamped to the active model's own limit.
let ctxEffective = null;
function applyCtxWindow(tokens) { listState.ctxWindow = tokens; $("ctxMax").textContent = `${Math.round(tokens / 1024)}k`; }
async function refreshContextWindow() {
  try { ctxEffective = (await api("/api/context-window")).effective; } catch { return; }
  if (engineState !== "ready" && ctxEffective) applyCtxWindow(ctxEffective);
}
async function poll() {
  try { health = await (await fetch("/api/llm/health")).json(); } catch { health = null; }
  const st = health?.status;
  engineState = st === "ok" ? "ready" : (st === "loading" || st === "starting") ? "loading" : "down";
  metrics = null;
  if (engineState === "ready") { try { metrics = await (await fetch("/api/llm/metrics")).json(); } catch {} }
  const dotCls = engineState === "ready" ? "ok" : engineState === "loading" ? "warn" : "bad";
  const emode = health?.mode, vision = emode === "vision";
  const label = engineState === "ready" ? (vision ? "Ready · Vision" : emode === "gguf" ? "Ready · llama.cpp" : "Ready") : engineState === "loading" ? (health?.phase === "warming_up" ? "Warming up…" : "Loading model…") : "Engine stopped";
  libraryState.runningName = engineState === "down" ? null : (health?.model_name || null);
  libraryState.runningMode = engineState === "down" ? null : emode;
  updateHeader();
  $("engName").textContent = libraryState.runningName || activeChat()?.name || "No model";
  $("mModel").textContent = libraryState.runningName || activeChat()?.name || "–";
  $("engDesc").textContent = emode === "gguf" ? "llama.cpp server (GGUF)" : vision ? "mlx-vlm server (images + text)" : "mlx-dspark with speculative decoding";
  const port = emode === "gguf" ? 8082 : vision ? 8081 : 8080;
  $("apiUrl").textContent = `http://127.0.0.1:${port}/v1`;
  for (const id of ["engDot", "hdrDot"]) $(id).className = "dot " + dotCls;
  $("hdrStatusTxt").textContent = label;
  const mean = metrics?.mean_decode_tokens_per_sec;
  $("engSt").textContent = engineState === "ready" && mean ? `${label} · ${(+mean).toFixed(0)} tok/s avg` : label;
  $("mmBadge").textContent = label; $("mmBadge").className = "badge " + (engineState === "ready" ? "ok" : engineState === "loading" ? "warn" : "");
  $("mmEngineTxt").textContent = engineState === "down" ? "Start engine" : "Stop engine";
  $("engToggleTxt").textContent = engineState === "down" ? "Start" : "Stop";
  $("mStatus").textContent = label;
  if (health && engineState === "ready") {
    $("eDr").textContent = (health.drafter || "none").split("/").pop(); $("eMode").textContent = emode === "gguf" ? "llama.cpp" : (health.mode || "–");
    $("eCap").textContent = health.max_draft ?? "–"; $("eKv").textContent = health.kv_bits ? `${health.kv_bits}-bit` : "bf16";
    applyCtxWindow(health.context_window || listState.ctxWindow);
    $("eWarn").textContent = (health.warnings || []).map((w) => w.message || w).join(" ");
  }
  else if (ctxEffective) applyCtxWindow(ctxEffective);
  if (metrics) {
    const m = metrics.memory || {}, s = metrics.system || {};
    $("eRes").textContent = m.active_bytes ? `${gib(m.active_bytes)} GB` : "–"; $("ePeak").textContent = m.peak_bytes ? `${gib(m.peak_bytes)} GB` : "–";
    $("ePress").textContent = s.pressure || "–"; $("eSwap").textContent = s.swap_used_bytes != null ? `${gib(s.swap_used_bytes)} GB` : "–";
    $("eReq").textContent = metrics.requests ?? "–"; $("eMean").textContent = mean ? `${(+mean).toFixed(1)} tok/s` : "–";
    $("mRes").textContent = m.active_bytes ? `${gib(m.active_bytes)} GB` : "–"; $("mSpeed").textContent = mean ? `${(+mean).toFixed(1)} tok/s` : "–"; $("mReq").textContent = metrics.requests ?? "–";
  } else { $("mRes").textContent = "–"; $("mSpeed").textContent = "–"; $("mReq").textContent = "–"; }
  try {
    const sys = await (await fetch("/api/system")).json();
    $("ramTxt").textContent = `${gib(sys.used_bytes)} / ${gib(sys.total_bytes)} GB`;
    $("ramBar").style.width = Math.min(100, sys.used_bytes / sys.total_bytes * 100) + "%";
  } catch {}
  updateSend();
}
setInterval(poll, 4000);
refreshContextWindow();
async function toggleEngine() {
  try {
    if (engineState === "down") { await jpost("/api/engine/start"); toast("Starting engine…"); }
    else { await jpost("/api/engine/stop"); toast("Engine stopped · memory released"); }
  } catch (e) { toast(e.message); }
  setTimeout(poll, 800);
}
$("mmEngine").onclick = () => { closeMenus(); toggleEngine(); };
$("engToggle").onclick = toggleEngine;

export {
  engineState, health, metrics, poll, refreshContextWindow, toggleEngine
};
