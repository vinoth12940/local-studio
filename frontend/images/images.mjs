// images/images.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { $, api, confirmBox, esc, gib, icon, jpost, toast } from "../modules.mjs";

// ---------------- images
$("advBtn").onclick = () => { $("adv").classList.toggle("on"); $("advBtn").classList.toggle("on"); };
$("dice").onclick = () => $("imgSeed").value = Math.floor(Math.random() * 2 ** 31);
$("imgPrompt").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(200, e.target.scrollHeight) + "px"; });
$("imgPrompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("imgGo").click(); });
let activeJob = null;
$("imgGo").onclick = async () => {
  const prompt = $("imgPrompt").value.trim(); if (!prompt) return $("imgPrompt").focus();
  const [w, h] = $("imgAspect").value.split("x"), fd = new FormData();
  Object.entries({ prompt, model_id: $("imgModel").value, width: w, height: h, steps: $("imgSteps").value, seed: $("imgSeed").value, guidance: $("imgGuidance").value, negative_prompt: $("imgNeg").value, step_cache: $("imgCache").value, mode: "generate" }).forEach(([k, v]) => fd.append(k, v));
  $("imgGo").disabled = true;
  try { const j = await api("/api/image", { method: "POST", body: fd }); activeJob = j.job; $("job").classList.add("on"); $("jobTitle").textContent = "Starting…"; $("jobSub").textContent = prompt; $("jobBar").style.width = "0"; $("jobPct").textContent = ""; track(j.job); }
  catch (e) { toast(e.message); $("imgGo").disabled = false; }
};
$("jobCancel").onclick = async () => { if (activeJob) { try { await jpost(`/api/image/${activeJob}/cancel`); toast("Cancelling…"); } catch (e) { toast(e.message); } } };
async function track(id) {
  let j; try { j = await api(`/api/image/${id}`); } catch { return setTimeout(() => track(id), 1500); }
  const pct = j.total ? j.step / j.total : 0;
  $("jobBar").style.width = (pct * 100) + "%"; $("jobPct").textContent = j.step ? `${Math.round(pct * 100)}%` : "";
  if (j.status === "queued") $("jobTitle").textContent = "Queued";
  if (j.status === "running") {
    let sub = `${Math.round(j.elapsed ?? 0)}s elapsed`;
    if (j.step > 1 && j.first_step_at) { const per = (Date.now() / 1000 - j.first_step_at) / (j.step - 1); sub += ` · about ${Math.max(1, Math.round(per * (j.total - j.step)))}s left · ${per.toFixed(1)}s per step`; }
    $("jobTitle").textContent = j.step ? `Generating · step ${j.step} of ${j.total}` : "Loading model…"; $("jobSub").textContent = sub;
  }
  if (["done", "error", "cancelled"].includes(j.status)) {
    $("job").classList.remove("on"); $("imgGo").disabled = false; activeJob = null;
    if (j.status === "done") { toast(`Image ready in ${Math.round(j.elapsed)}s`); loadGallery(); }
    if (j.status === "error") toast(`Generation failed: ${(j.log || []).slice(-1)[0] || "exit " + j.returncode}`);
    if (j.status === "cancelled") toast("Generation cancelled");
    return;
  }
  setTimeout(() => track(id), 1000);
}
let gallery = [];
async function loadGallery() {
  try { gallery = await api("/api/gallery"); } catch { gallery = []; }
  $("gCount").textContent = gallery.length ? `${gallery.length} image${gallery.length > 1 ? "s" : ""}` : "";
  $("gallery").innerHTML = gallery.length ? gallery.map((m, i) => `<div class="gitem" data-i="${i}"><img src="${m.url}" loading="lazy" alt="">
    <div class="tools"><a href="${m.url}" download title="Download" data-stop>${icon("i-dl")}</a><button data-del="${i}" title="Delete">${icon("i-trash")}</button></div>
    <div class="hov"><p>${esc(m.prompt)}</p><div class="s">${esc(m.model_name || "Qwen-Image 2.1")} · ${m.width}×${m.height} · ${m.steps} steps${m.elapsed ? " · " + Math.round(m.elapsed) + "s" : ""}</div></div></div>`).join("")
    : `<div class="empty">No images yet. Describe one above to get started.</div>`;
}
$("gallery").onclick = async (e) => {
  if (e.target.closest("[data-stop]")) return;
  const del = e.target.closest("[data-del]");
  if (del) { e.stopPropagation(); await deleteImage(gallery[+del.dataset.del]); return; }
  const it = e.target.closest(".gitem"); if (it) openLightbox(gallery[+it.dataset.i]);
};
async function deleteImage(m) {
  if ($("lbDlg").open) $("lbDlg").close();
  if (!(await confirmBox("Delete image?", "This image and its settings will be permanently deleted."))) return;
  await api(`/api/gallery/${encodeURIComponent(m.file)}`, { method: "DELETE" }); toast("Image deleted"); loadGallery();
}
let lbItem = null;
function openLightbox(m) {
  lbItem = m; $("lbImg").src = m.url; $("lbDl").href = m.url; $("lbPrompt").textContent = m.prompt;
  const rows = [["Model", m.model_name || "Qwen-Image 2.1"], ["Size", `${m.width} × ${m.height}`], ["Steps", m.steps], ["Seed", m.seed], ["Time", m.elapsed ? `${m.elapsed}s` : null], ["Per step", m.sec_per_step ? `${m.sec_per_step}s` : null],
    ["Peak memory", m.peak_bytes ? `${gib(m.peak_bytes)} GB` : null], ["Guidance", m.guidance], ["Step cache", m.step_cache_ratio], ["Created", m.created ? new Date(m.created * 1000).toLocaleString() : null]];
  $("lbKv").innerHTML = rows.filter(([, v]) => v != null && v !== "").map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  $("lbDlg").showModal();
}
$("lbClose").onclick = () => $("lbDlg").close();
$("lbDlg").onclick = (e) => { if (e.target === $("lbDlg")) $("lbDlg").close(); };
$("lbDel").onclick = () => deleteImage(lbItem);
$("lbReuse").onclick = () => {
  const m = lbItem; $("lbDlg").close();
  $("imgPrompt").value = m.prompt; $("imgSeed").value = m.seed; $("imgGuidance").value = m.guidance ?? ""; $("imgNeg").value = m.negative_prompt ?? ""; $("imgCache").value = m.step_cache_ratio ?? "";
  const size = `${m.width}x${m.height}`; if ([...$("imgAspect").options].some((o) => o.value === size)) $("imgAspect").value = size;
  if ([...$("imgSteps").options].some((o) => o.value == m.steps)) $("imgSteps").value = m.steps;
  $("imgPrompt").focus(); document.querySelector("#v-images .scroll").scrollTop = 0;
};

export {
  activeJob, deleteImage, gallery, lbItem, loadGallery, openLightbox, track
};
