// The UI's entry point: index.html loads it as a module. What is not split into frontend/ yet still lives here,
// in its original order (the refactor's P2 split, docs/refactor-progress.json).
import "./globals.mjs";
import {
  $, api, loadAgent, loadChats, loadLibrary, loadSearch, loadWorkflow, poll, renderThread, showView, threadState
} from "./modules.mjs";

// ---------------- boot
(async () => {
  await api("/api/system").then((s) => { $("verBadge").textContent = s.version; threadState.sysRam = s.total_bytes; }).catch(() => {});
  await loadLibrary();
  loadSearch();
  loadAgent();
  showView("chat");
  renderThread();
  await loadChats();
  await loadWorkflow();
  poll();
})();
