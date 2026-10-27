// Explicit task controls. Mode/permission state is always validated by the server.
import { $, api, esc, jpost, newId, setOptions, toast } from "../core/util.mjs";
import { listState, saveChat } from "./list.mjs";
import { activeCodeWs, generate, renderThread } from "../modules.mjs";

const FEATURE_LABELS = { workflow: "Plan / Act / Auto", durable_runs: "Restart recovery", verification_gate: "Required checks",
  verification_fingerprints: "Detect changed source", nested_instructions: "Scoped project instructions", skills: "Skills",
  hooks: "Tool hooks", protect_tests: "Protect test files", review: "Independent reviews", triggers: "Scheduled tasks", worktrees: "Isolated write workers" };
const workflowState = { enabled: false, features: {}, workspace: null, tasks: [], pending: false, mode: "plan", run: null };
const selectedTask = () => workflowState.tasks.find((t) => t.id === listState.current?.workflow_task_id);
const workspace = () => listState.current?.workspace || activeCodeWs() || null;
function showError(error) { const el = $("workflowError"); el.hidden = !error; el.textContent = error?.message || error || ""; }

function syncWorkflow() {
  const ws = workspace(), task = selectedTask();
  $("workflowBar").hidden = !workflowState.enabled || !ws;
  setOptions($("workflowTaskSel"), `<option value="">No coding task</option>` + workflowState.tasks.map((t) => `<option value="${esc(t.id)}">${esc((t.artifacts?.["intent.md"]?.version ? "Task " : "") + t.id.slice(0, 8))} · ${esc(t.state)}</option>`).join(""), task?.id || "");
  $("workflowModeSel").value = task?.mode || listState.current?.workflow_mode || workflowState.mode;
  $("workflowModeSel").disabled = !!listState.streaming;
  $("workflowPause").hidden = !task || task.state !== "running";
  $("workflowResume").hidden = !task || !["paused", "cancelled", "needs_user", "planned", "incomplete"].includes(task.state);
  const queued = [workflowState.ready ? `${workflowState.ready} scheduled ready` : "", workflowState.attention ? `${workflowState.attention} need attention` : ""].filter(Boolean).join(" · ");
  $("workflowStatus").textContent = (task ? `${task.state} · ${task.usage?.steps || 0}/${task.budgets.max_steps} tools` : "Select or create a task") + (queued ? ` · ${queued}` : "");
  if (ws !== workflowState.workspace && !workflowState.pending) refreshWorkflow().catch(() => {});
}

async function loadWorkflow() {
  try { Object.assign(workflowState, await api("/api/workflow/status")); await refreshWorkflow(); }
  catch { workflowState.enabled = false; syncWorkflow(); }
}

async function refreshWorkflow() {
  const ws = workspace();
  if (workflowState.pending) return;
  workflowState.pending = true;
  try {
    workflowState.workspace = ws;
    workflowState.tasks = workflowState.enabled && ws ? await api(`/api/workflow/tasks?workspace=${encodeURIComponent(ws)}`) : [];
    if (workflowState.enabled && workflowState.features.triggers) {          // evaluating schedules also happens server-side on this call
      const note = await api("/api/workflow/notifications").catch(() => null);
      workflowState.ready = note?.ready || 0; workflowState.attention = note?.notices?.length || 0;
    } else { workflowState.ready = 0; workflowState.attention = 0; }
  } finally { workflowState.pending = false; syncWorkflow(); }
}

function workflowRequest(chat) {
  if (!workflowState.enabled || !chat.workspace) return {};
  const mode = selectedTask()?.mode || chat.workflow_mode || workflowState.mode;
  if (chat.workflow_task_id && !selectedTask()) throw new Error("The selected task is unavailable; reload workflow details.");
  if (mode === "auto" && !chat.workflow_task_id) throw new Error("Create a scoped task before using Auto.");
  return { workflow_mode: mode, ...(chat.workflow_task_id ? { workflow_task_id: chat.workflow_task_id } : {}),
    ...(workflowState.features.durable_runs || chat.workflow_task_id ? { workflow_request_key: newId() + newId() } : {}) };
}

async function fillTask(task) {
  $("workflowTaskInfo").textContent = task ? `${task.id} · revision ${task.version} · ${task.state}` : (workspace() || "Choose a workspace first.");
  for (const [id, name] of [["workflowIntent", "intent.md"], ["workflowSpec", "spec.md"], ["workflowPlan", "plan.md"]]) {
    $(id).value = task ? (await api(`/api/workspace/file?workspace=${encodeURIComponent(task.workspace)}&path=${encodeURIComponent(task.artifacts[name].path)}`)).content : "";
  }
  $("workflowScope").value = (task?.scope || ["**"]).join("\n");
  $("workflowSteps").value = task?.budgets.max_steps || 48;
  $("workflowSeconds").value = task?.budgets.max_seconds || 900;
  $("workflowReviewRequired").checked = !!task?.review_required;
  renderReview(task).catch((error) => { $("workflowReviewInfo").textContent = "Review state is unavailable."; showError(error); });
  $("workflowSaveArtifacts").disabled = !task;
  $("workflowAdopt").disabled = !task;
  $("workflowRunReview").disabled = !task || !workflowState.features.review;
}

const DISPOSITIONS = [["open", "Open"], ["fix_requested", "Fix requested"], ["rejected", "Reject finding"], ["accepted_risk", "Accept risk"]];
const SEVERITY_CLASS = { critical: "err", high: "err", medium: "err" };

// Authoritative review state lives on the server; this only draws it. Dispositions are explicit user actions.
async function renderReview(task) {
  const info = $("workflowReviewInfo"), box = $("workflowFindings");
  box.innerHTML = "";
  if (!task) { info.textContent = "No independent review recorded."; return; }
  if (!workflowState.features.review) { info.textContent = task.review ? `Review: ${task.review.status}` : "Independent reviews are off."; return; }
  const state = await api(`/api/workflow/tasks/${encodeURIComponent(task.id)}/review`);
  const review = state.review;
  if (!review) { info.textContent = "No independent review recorded."; return; }
  const stale = state.current ? "" : ` · not current${state.reason ? ": " + state.reason : ""}`;
  info.textContent = `Review ${review.status} · ${review.findings.length} finding(s)${stale}`;
  const summary = review.summary || review.error || "";
  box.innerHTML = (summary ? `<p class="sub">${esc(summary)}</p>` : "") + review.findings.map((f) => `<div class="workflow-finding" data-finding="${esc(f.id)}">
    <b class="${SEVERITY_CLASS[f.severity] || ""}">${esc(f.severity)}${f.blocking && ["open", "fix_requested"].includes(f.disposition) ? " · blocks completion" : ""}</b> ${esc(f.title)}
    <div class="sub">${esc(f.path)}:${esc(String(f.line))} (${esc(f.side)}) · ${esc(f.disposition)}${f.owner ? " · owner " + esc(f.owner) : ""}</div>
    <pre>${esc(f.evidence)}</pre>
    ${f.disposition_reason ? `<div class="sub">Reason: ${esc(f.disposition_reason)}</div>` : ""}
    <select aria-label="Disposition for ${esc(f.id)}" data-finding-disposition>${DISPOSITIONS.map(([v, l]) => `<option value="${v}" ${v === f.disposition ? "selected" : ""}>${l}</option>`).join("")}</select>
    <input aria-label="Reason for ${esc(f.id)}" data-finding-reason placeholder="Reason (required)">
    <button class="btn sm" data-finding-save ${state.current ? "" : "disabled"}>Record decision</button></div>`).join("");
}

// The newest run for this task whose backend snapshot is verified; the server re-checks that it is current.
async function verifiedRunFor(task) {
  const { runs } = await api("/api/workflow/runs");
  const candidates = (runs || []).filter((r) => r.metadata?.task_id === task.id).sort((a, b) => b.updated - a.updated).slice(0, 8);
  for (const candidate of candidates) {
    const run = await api(`/api/workflow/runs/${encodeURIComponent(candidate.id)}`);
    if (run.verification?.status === "verified") return run.id;
  }
  return null;
}

// Scheduled tasks: the server only queues invocations; starting one is an explicit, attended action.
const scheduleLabel = (t) => !t.schedule ? "external calls only" : t.schedule.type === "interval" ? `every ${Math.round(t.schedule.every_seconds / 60)} min` : `daily at ${t.schedule.at}`;
const INVOCATION_LABEL = { pending: "ready to start", started: "running", completed: "completed", failed: "failed", needs_user: "needs your attention", cancelled: "cancelled", skipped: "skipped", interrupted: "interrupted", blocked: "not started" };

async function renderTriggers() {
  const list = $("workflowTriggers"), notices = $("workflowNotices");
  if (!workflowState.features.triggers || !workspace()) { list.textContent = workflowState.features.triggers ? "Choose a workspace first." : "Scheduled tasks are off. Enable them in the feature switches above."; notices.innerHTML = ""; return; }
  const [{ triggers }, note] = await Promise.all([api(`/api/workflow/triggers?workspace=${encodeURIComponent(workspace())}`), api("/api/workflow/notifications")]);
  workflowState.triggerReady = note.ready;
  $("workflowStatus").title = note.ready ? `${note.ready} scheduled run(s) ready to start` : "";
  notices.innerHTML = note.notices.map((n) => `<div class="workflow-notice" data-notice="${esc(n.id)}">${esc(n.notice)} <button class="linkbtn" data-notice-ack="${esc(n.id)}">Dismiss</button></div>`).join("");
  list.innerHTML = triggers.map((t) => `<div class="workflow-trigger" data-trigger="${esc(t.id)}" data-version="${t.version}">
    <b>${esc(t.name)}</b> · ${esc(scheduleLabel(t))} · ${t.enabled ? "on" : "paused"} · missed: ${esc(t.missed_policy === "skip" ? "skipped" : "one catch-up")}
    <div class="sub">${esc(t.prompt)}</div>
    ${t.invocations.slice(-4).map((i) => `<div class="sub" data-invocation="${esc(i.id)}">${esc(INVOCATION_LABEL[i.status] || i.status)}${i.reason ? " — " + esc(i.reason) : ""}
      ${i.status === "pending" ? `<button class="btn sm" data-invocation-start>Start with approvals</button><button class="linkbtn" data-invocation-cancel>Cancel</button>` : ""}</div>`).join("")}
    <button class="linkbtn" data-trigger-toggle="${t.enabled ? 0 : 1}">${t.enabled ? "Pause" : "Resume"}</button><button class="linkbtn" data-trigger-delete>Delete</button></div>`).join("") || "No scheduled tasks for this workspace.";
}

async function startInvocation(id) {
  if (listState.streaming) throw new Error("Stop the current reply before starting a scheduled run.");
  const { request } = await jpost(`/api/workflow/invocations/${encodeURIComponent(id)}/start`, {});
  const chat = { id: newId(), title: "Scheduled task", workspace: request.workspace, workflow_task_id: request.workflow_task_id, messages: [{ role: "user", content: request.prompt }] };
  listState.current = chat;
  await saveChat();
  workflowState.resume = { workflow_request_key: request.workflow_request_key };
  $("workflowDlg").close(); renderThread(); await generate(); await refreshWorkflow();
}

function scheduleFromForm() {
  const kind = $("triggerSchedule").value, value = $("triggerValue").value.trim();
  if (!kind) return null;
  if (kind === "interval") { const minutes = Number(value); if (!Number.isInteger(minutes) || minutes < 1) throw new Error("Enter a whole number of minutes (at least 1)."); return { type: "interval", every_seconds: minutes * 60 }; }
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) throw new Error("Enter the time as HH:MM, for example 09:30."); return { type: "daily", at: value };
}

// Isolated workers: every state change is an explicit user action; integration is bound to the reviewed digest.
async function renderWorkers() {
  const box = $("workflowWorkers"), task = selectedTask();
  $("workerReview").innerHTML = "";
  if (!workflowState.features.worktrees) { box.textContent = "Isolated workers are off. Enable them in the feature switches above."; return; }
  if (!task) { box.textContent = "Select a coding task first."; return; }
  const { workers, orphaned_directories: orphans } = await api(`/api/workflow/worktrees?task_id=${encodeURIComponent(task.id)}`);
  box.innerHTML = workers.map((w) => `<div class="workflow-trigger" data-worker="${esc(w.id)}"><b>${esc(w.owner)}</b> · ${esc(w.state)}${w.health === "missing" ? " · checkout missing" : ""} · ${esc(w.scope.join(", "))}
    <div class="sub">Open a Code chat on <code>${esc(w.path)}</code> to let this worker edit. Branch ${esc(w.branch)}. ${w.runs.length} run(s) linked · ${w.steps_used || 0}/${w.budgets.max_steps} tool steps charged.</div>
    ${w.state === "active" ? `<button class="btn sm" data-worker-review>Review changes</button><button class="linkbtn" data-worker-abandon>Abandon</button>` : ""}
    ${w.state !== "active" && !w.cleaned ? `<button class="linkbtn" data-worker-cleanup>Clean up checkout</button>` : ""}</div>`).join("") + (orphans.length ? `<p class="sub">Unregistered directories found: ${orphans.map(esc).join(", ")}</p>` : "") || "No workers for this task.";
}

async function reviewWorker(id) {
  const r = await api(`/api/workflow/worktrees/${encodeURIComponent(id)}/integration`);
  $("workerReview").innerHTML = `<div class="workflow-finding" data-review-worker="${esc(id)}" data-digest="${esc(r.digest)}"><b>${r.can_integrate ? "Ready to apply" : "Blocked"}</b> · ${r.files.length} file(s)${r.stale_base ? " · main moved since start" : ""}
    ${r.problems.map((p) => `<div class="err">${esc(p)}</div>`).join("")}<div class="sub">${r.files.map(esc).join(", ")}</div><pre>${esc(r.patch_preview)}${r.patch_truncated ? "\n… truncated" : ""}</pre>
    <button class="btn sm primary" data-worker-integrate ${r.can_integrate ? "" : "disabled"}>Apply this patch to my workspace (uncommitted)</button></div>`;
}

async function openWorkflow() {
  showError(null);
  await loadWorkflow();
  $("workflowFeatures").innerHTML = Object.entries(FEATURE_LABELS).map(([key, label]) => `<label><input type="checkbox" data-workflow-feature="${key}" ${workflowState.features[key] ? "checked" : ""}>${esc(label)}</label>`).join("");
  try {
    await fillTask(selectedTask());
    const checks = workspace() ? await api(`/api/workflow/checks?workspace=${encodeURIComponent(workspace())}`) : { commands: [] };
    $("workflowChecks").value = (checks.commands || []).map((c) => `${c.id}: ${c.command}`).join("\n");
    await renderWorkflowRuns();
    await renderTriggers();
    await renderWorkers();
  } catch (error) { showError(error); }
  $("workflowDlg").showModal();
}

async function changeTask(changes) {
  const task = selectedTask(); if (!task) throw new Error("Select a task first.");
  const value = await jpost(`/api/workflow/tasks/${task.id}`, { expected_version: task.version, changes }, "PATCH");
  workflowState.tasks = workflowState.tasks.map((t) => t.id === value.id ? value : t);
  syncWorkflow(); return value;
}

async function renderWorkflowRuns() {
  const container = $("workflowRuns");
  if (!workflowState.features.durable_runs && !workflowState.enabled) { container.textContent = "Restart recovery is disabled."; return; }
  const result = await api("/api/workflow/runs");
  container.innerHTML = (result.runs || []).filter((r) => r.workspace === workspace()).slice(0, 12).map((r) => `<button class="btn sm" data-workflow-run="${esc(r.id)}">${esc(r.id.slice(0, 8))} · ${esc(r.phase)}</button>`).join(" ") || "No recorded runs for this workspace.";
}

async function showWorkflowRun(id) {
  const run = await api(`/api/workflow/runs/${encodeURIComponent(id)}`);
  workflowState.run = run;
  const unknown = Object.entries(run.steps || {}).filter(([, step]) => step.status === "unknown_outcome");
  $("workflowRunDetail").innerHTML = `<p>${esc(run.id)} · ${esc(run.phase)}</p>` + unknown.map(([key, step]) => `<div class="workflow-unknown"><b>${esc(step.name)} — outcome unknown</b><p>The process ended before its result was saved. Inspect its effects before continuing.</p><input aria-label="Reconciliation evidence" data-reconcile-evidence="${esc(key)}" placeholder="What did you inspect?"><select data-reconcile-disposition="${esc(key)}"><option value="executed">Executed; preserve its effects</option><option value="not_executed">Not executed; it may be retried</option><option value="abandon">Abandon this run</option></select><button class="btn sm" data-reconcile-step="${esc(key)}">Record decision</button></div>`).join("") + `<div class="btns"><button class="btn" id="workflowCancelRun">Cancel run</button><button class="btn" id="workflowContinueRun" ${unknown.length ? "disabled" : ""}>Continue with fresh approvals</button></div>`;
  $("workflowCancelRun").onclick = async () => { try { await jpost(`/api/workflow/runs/${run.id}/cancel`, {}); await showWorkflowRun(run.id); } catch (e) { showError(e); } };
  $("workflowContinueRun").onclick = async () => {
    try {
      if (listState.streaming) throw new Error("Stop the current reply before continuing a saved run.");
      const prepared = await jpost(`/api/workflow/runs/${run.id}/resume`, { expected_seq: run.seq });
      if (!listState.current) listState.current = { id: newId(), title: "Recovered coding task", workspace: run.workspace, messages: [] };
      listState.current.workspace = run.workspace;
      if (run.metadata?.task_id) listState.current.workflow_task_id = run.metadata.task_id;
      workflowState.resume = prepared.next_request;
      $("workflowDlg").close(); renderThread(); await generate(); await refreshWorkflow();
    } catch (error) { showError(error); }
  };
}

function takeWorkflowResume() { const value = workflowState.resume; delete workflowState.resume; return value; }

$("codeWorkflow").onclick = () => openWorkflow().catch(showError);
$("workflowOpen").onclick = () => openWorkflow().catch(showError);
$("workflowDone").onclick = () => $("workflowDlg").close();
$("workflowSaveFeatures").onclick = async () => {
  try { const changes = {}; $("workflowFeatures").querySelectorAll("[data-workflow-feature]").forEach((el) => changes[el.dataset.workflowFeature] = el.checked); Object.assign(workflowState, await jpost("/api/workflow/settings", changes)); await refreshWorkflow(); toast("Workflow settings saved"); showError(null); } catch (e) { showError(e); }
};
$("workflowSaveChecks").onclick = async () => {
  try { const commands = $("workflowChecks").value.split("\n").filter((line) => line.trim()).map((line) => { const split = line.indexOf(":"); if (split < 1) throw new Error("Use name: command on each line."); return { id: line.slice(0, split).trim(), command: line.slice(split + 1).trim() }; }); await jpost("/api/workflow/checks", { workspace: workspace(), commands }); toast("Required checks saved"); showError(null); } catch (e) { showError(e); }
};
$("workflowNewTask").onclick = async () => {
  try {
    const task = await jpost("/api/workflow/tasks", { workspace: workspace(), intent: $("workflowIntent").value, spec: $("workflowSpec").value, plan: $("workflowPlan").value,
      mode: $("workflowModeSel").value, scope: $("workflowScope").value.split("\n").filter(Boolean),
      budgets: { max_steps: +$("workflowSteps").value, max_seconds: +$("workflowSeconds").value }, review_required: $("workflowReviewRequired").checked });
    if (!listState.current) listState.current = { id: newId(), title: "Coding task", workspace: task.workspace, messages: [] };
    listState.current.workflow_task_id = task.id; await saveChat(); await refreshWorkflow(); await fillTask(task); showError(null); toast("Task and planning artifacts created");
  } catch (e) { showError(e); }
};
$("workflowSaveArtifacts").onclick = async () => {
  try { const task = await changeTask({ artifacts: { "intent.md": $("workflowIntent").value, "spec.md": $("workflowSpec").value, "plan.md": $("workflowPlan").value }, scope: $("workflowScope").value.split("\n").filter(Boolean), review_required: $("workflowReviewRequired").checked }); await fillTask(task); showError(null); } catch (e) { showError(e); }
};
$("workflowAdopt").onclick = async () => { try { await fillTask(await changeTask({ adopt_artifacts: true })); showError(null); } catch (e) { showError(e); } };
$("workflowRunReview").onclick = async () => {
  const button = $("workflowRunReview");
  try {
    const task = selectedTask(); if (!task) throw new Error("Select a task first.");
    if (listState.streaming) throw new Error("Stop the current reply before starting a review.");
    const runId = await verifiedRunFor(task);
    if (!runId) throw new Error("Run the required checks to a verified result in this task first; reviews need current verification evidence.");
    button.disabled = true; $("workflowReviewInfo").textContent = "Reviewing with the loaded local model…";
    const result = await jpost(`/api/workflow/tasks/${task.id}/review`, { expected_version: task.version, verification_run_id: runId });
    workflowState.tasks = workflowState.tasks.map((t) => t.id === result.task.id ? result.task : t);
    syncWorkflow(); await fillTask(result.task);
    showError(result.review.error ? new Error(result.review.error) : null);
  } catch (e) { showError(e); await fillTask(selectedTask()).catch(() => {}); }
  finally { button.disabled = !selectedTask() || !workflowState.features.review; }
};
$("workflowFindings").onclick = async (event) => {
  const button = event.target.closest("[data-finding-save]"); if (!button) return;
  const row = button.closest("[data-finding]"), task = selectedTask();
  try {
    const result = await jpost(`/api/workflow/tasks/${task.id}/review/findings/${encodeURIComponent(row.dataset.finding)}`,
      { expected_version: task.version, disposition: row.querySelector("[data-finding-disposition]").value, reason: row.querySelector("[data-finding-reason]").value }, "PATCH");
    workflowState.tasks = workflowState.tasks.map((t) => t.id === result.task.id ? result.task : t);
    syncWorkflow(); await fillTask(result.task); showError(null);
  } catch (e) { showError(e); }
};
$("triggerCreate").onclick = async () => {
  try {
    const task = selectedTask(); if (!task) throw new Error("Select a coding task first; a scheduled task reuses its scope and budgets.");
    const created = await jpost("/api/workflow/triggers", { workspace: workspace(), task_id: task.id, name: $("triggerName").value, prompt: $("triggerPrompt").value, schedule: scheduleFromForm(), missed_policy: $("triggerMissed").value });
    const note = $("triggerToken"); note.hidden = false;
    note.textContent = created.trigger.schedule ? "Created." : `Created. Token (shown once): ${created.token}  — send it as "Authorization: Bearer <token>" to POST /api/agent/trigger with {"trigger_id": "${created.trigger.id}", "idempotency_key": "..."}.`;
    $("triggerName").value = ""; $("triggerPrompt").value = ""; await renderTriggers(); showError(null);
  } catch (e) { showError(e); }
};
$("workflowTriggers").onclick = async (event) => {
  const trigger = event.target.closest("[data-trigger]"); if (!trigger) return;
  try {
    const invocation = event.target.closest("[data-invocation]")?.dataset.invocation;
    if (event.target.closest("[data-invocation-start]")) { await startInvocation(invocation); return; }
    if (event.target.closest("[data-invocation-cancel]")) await jpost(`/api/workflow/invocations/${invocation}/cancel`, {});
    else if (event.target.closest("[data-trigger-toggle]")) await jpost(`/api/workflow/triggers/${trigger.dataset.trigger}`, { expected_version: +trigger.dataset.version, enabled: event.target.closest("[data-trigger-toggle]").dataset.triggerToggle === "1" }, "PATCH");
    else if (event.target.closest("[data-trigger-delete]")) await api(`/api/workflow/triggers/${trigger.dataset.trigger}`, { method: "DELETE" });
    else return;
    await renderTriggers(); showError(null);
  } catch (e) { showError(e); await renderTriggers().catch(() => {}); }
};
$("workflowNotices").onclick = async (event) => {
  const button = event.target.closest("[data-notice-ack]"); if (!button) return;
  try { await jpost(`/api/workflow/invocations/${button.dataset.noticeAck}/ack`, {}); await renderTriggers(); } catch (e) { showError(e); }
};
$("workerCreate").onclick = async () => {
  try {
    const task = selectedTask(); if (!task) throw new Error("Select a coding task first.");
    const scope = $("workerScope").value.split(",").map((x) => x.trim()).filter(Boolean);
    await jpost("/api/workflow/worktrees", { task_id: task.id, owner: $("workerOwner").value, scope });
    $("workerOwner").value = ""; $("workerScope").value = ""; await renderWorkers(); showError(null);
  } catch (e) { showError(e); }
};
$("workflowWorkers").onclick = async (event) => {
  const row = event.target.closest("[data-worker]"); if (!row) return;
  const id = row.dataset.worker;
  try {
    if (event.target.closest("[data-worker-review]")) { await reviewWorker(id); showError(null); return; }
    if (event.target.closest("[data-worker-abandon]")) await jpost(`/api/workflow/worktrees/${id}/abandon`, {});
    else if (event.target.closest("[data-worker-cleanup]")) await jpost(`/api/workflow/worktrees/${id}/cleanup`, {});
    else return;
    await renderWorkers(); showError(null);
  } catch (e) { showError(e); }
};
$("workerReview").onclick = async (event) => {
  const box = event.target.closest("[data-review-worker]"); if (!box || !event.target.closest("[data-worker-integrate]")) return;
  try {
    const done = await jpost(`/api/workflow/worktrees/${box.dataset.reviewWorker}/integrate`, { expected_digest: box.dataset.digest });
    toast("Patch applied, not committed. " + done.next); await renderWorkers(); showError(null);
  } catch (e) { showError(e); }
};
$("workflowPause").onclick = async () => { try { await changeTask({ state: "paused" }); listState.streaming?.abort(); } catch (e) { toast(e.message); } };
$("workflowResume").onclick = async () => { try { await changeTask({ state: "ready" }); } catch (e) { toast(e.message); } };
$("workflowTaskSel").onchange = async () => { if (!listState.current) return; listState.current.workflow_task_id = $("workflowTaskSel").value || null; await saveChat(); syncWorkflow(); };
$("workflowModeSel").onchange = async () => {
  try { const mode = $("workflowModeSel").value; if (selectedTask()) await changeTask({ mode }); else { if (mode === "auto") throw new Error("Create a scoped task before using Auto."); workflowState.mode = mode; if (listState.current) { listState.current.workflow_mode = mode; await saveChat(); } } syncWorkflow(); } catch (e) { toast(e.message); syncWorkflow(); }
};
$("workflowRuns").onclick = (event) => { const button = event.target.closest("[data-workflow-run]"); if (button) showWorkflowRun(button.dataset.workflowRun).catch(showError); };
$("workflowRunDetail").onclick = async (event) => {
  const button = event.target.closest("[data-reconcile-step]"); if (!button || !workflowState.run) return;
  try { const key = button.dataset.reconcileStep; const row = button.closest(".workflow-unknown"); const run = workflowState.run;
    await jpost(`/api/workflow/runs/${run.id}/reconcile`, { expected_seq: run.seq, step_id: key, disposition: row.querySelector("select").value, evidence: row.querySelector("input").value }); await showWorkflowRun(run.id); showError(null);
  } catch (e) { showError(e); }
};
// Scheduled work is only useful if someone notices it: poll while triggers are on (unref'd so plain node can exit).
const poll = setInterval(() => { if (workflowState.enabled && workflowState.features.triggers && !document.hidden) refreshWorkflow().catch(() => {}); }, 60000);
poll?.unref?.();
export { loadWorkflow, openWorkflow, refreshWorkflow, syncWorkflow, takeWorkflowResume, workflowRequest, workflowState };
