// agent/state.mjs: moved from index.html's inline script in the refactor's P2 split (docs/refactor-progress.json).
import { $ } from "../core/util.mjs";
import { agSync, loadAgent, loadAgentRuns } from "../modules.mjs";

// One run's state. The Agent view keeps a single one (`ag`); a task started from chat keeps its own (`chRun.s`); every agXxx(s, …) below works on either.
function agFresh() {
  return { rid: null, events: [], last: -1, live: false, retries: 0, cards: {}, pending: null, paused: false, pausing: false, stopping: false, replyingN: null,
    engine: "", shot: null, where: "", urls: [], answer: "", task: "", status: "", reason: "", lost: false, chatId: null };
}
const ag = { ...agFresh(), st: null, runs: [], es: null, pinned: false, replay: false, timer: 0, shotShown: null, setSig: "", runsSig: "", bgWait: false,
  log: $("agLog"), scroller: () => document.querySelector("#v-agent .scroll"),
  hooks: { sync: () => agSync(), ended: () => { loadAgentRuns(); loadAgent(); }, retry: () => loadAgent() } };

const agPermMissing = (st) => st?.permissions?.accessibility === false || st?.permissions?.screen_recording === false;

export {
  ag, agFresh, agPermMissing
};
