// voice/turn.mjs: when a spoken turn is finished, what counts as an acknowledgement, and how a streamed reply is cut
// into speakable pieces. Pure (no DOM), so plain node can import it: macos/check_voice_ui.mjs scores it on labeled phrases.
// Ported unchanged from the voice POC (build/voice-poc, eval.mjs: 100% / 100% / 93% on its three phrase sets).

const BC_PHRASES = new Set(['i see', 'got it', 'uh huh', 'mm hm', 'mm hmm', 'oh i see', 'oh okay', 'ah okay', 'oh wow', 'oh nice', 'ok cool', 'okay cool',
  'okay great', 'right right', 'yeah yeah', 'yes yes', 'oh really', 'oh cool', 'i understand']);
const BC_WORDS = new Set(['yeah', 'yes', 'yep', 'yup', 'mm', 'mmm', 'mhm', 'mmhmm', 'hmm', 'uh', 'huh', 'okay', 'ok', 'right', 'sure', 'cool', 'wow', 'really',
  'nice', 'great', 'alright', 'gotcha', 'oh', 'ah', 'interesting', 'true', 'exactly', 'fine', 'indeed']);
function isBackchannel(text) {   // "yeah", "uh-huh", "I see", "got it": acknowledging, not taking the floor
  const t = text.toLowerCase().replace(/[^a-z' ]/g, ' ').replace(/\s+/g, ' ').trim(); if (!t) return false;
  if (BC_PHRASES.has(t)) return true;
  const w = t.split(' '); return w.length <= 3 && w.every(x => BC_WORDS.has(x));
}
const TRAILING = /\b(and|but|or|because|if|then|which|who|when|while|with|without|to|of|for|in|on|at|from|about|into|the|a|an|my|your|his|their|our|its|these|those|um|uh|er|erm|like|also|plus|whether|than|as|by|what|where|how|why|let's)$/i;
const SHORT_COMPLETE = /^(?:(?:yes|no|yeah|yep|nope|okay|ok|stop|wait|hello|hi|hey|thanks|thank you|sure|please|go on|continue|exactly|right|correct|never ?mind|cancel|repeat that|again|good (?:morning|evening|night|afternoon)|goodbye|bye|see you|welcome back)\W*\s*)+$/i;
const THAT_OPEN = /\b(?:think|thinking|mean|means|said|say|says|told (?:me|you|him|her|them)|tell (?:me|you)|wondering|wonder|feel|believe|hope|guess|suppose|realize|remember) that$/i;
const OPEN_PATTERNS = [   // sentence frames that are not finished until something follows
  /\b(?:reason|thing|problem|point|idea|question|issue|trouble|answer|difference|trick|catch|happened|matters|mean|meant)\b.*\b(?:is|was|are)(?: that)?$/i,
  /\b(?:wondering|wonder|asking|curious|sure|know) (?:if|whether)\b.*\b(?:could|can|would|might|will|should)$/i,
  /\b(?:if|when|how|whether) (?:you|i|we|they|he|she|it) (?:could|can|would|will|should|might|were|was)$/i,
];
const PREPS = /\b(at|for|about|with|to|in|on|from|of|into)$/i;
const WH_START = /^(what|who|whom|where|when|why|how|which)\b/i;
const NATURAL_END = /\b(so|too|it|you|me|him|them|there|here|now|again|today|tomorrow|tonight|please|either|anyway|though)$/i;
const IMPERATIVE = /^(tell|show|open|find|search|play|set|call|remind|give|read|write|make|turn|check|look|send|help|explain|list|summarize|stop|start|take|bring|get|put|pause|resume|read|describe)\b/i;
function endpointWait(text) {   // -> [ms to wait for you to carry on before answering, why]
  const s = text.trim().replace(/\s+/g, ' '), words = s ? s.split(' ').length : 0;
  if (!words) return [0, 'empty'];
  if (/\?["')]*$/.test(s)) return [80, 'question mark'];
  if (/(,|;|:|-|…|\.\.\.)$/.test(s)) return [1500, 'trailing comma or pause'];
  const bare = s.replace(/[.!"')]+$/, '');
  const ended = /[.!]["')]*$/.test(s);
  if (!ended && WH_START.test(s) && words >= 4 && PREPS.test(bare)) return [350, 'question ending in a preposition'];
  if (!ended && (TRAILING.test(bare) || THAT_OPEN.test(bare) || (words >= 4 && OPEN_PATTERNS.some(re => re.test(bare))))) return [1600, 'ends mid-thought ("' + bare.split(' ').pop() + '")'];
  if (SHORT_COMPLETE.test(s)) return [150, 'short complete reply'];
  if (/[.!]["')]*$/.test(s)) return [100, 'sentence end'];
  if (words >= 2 && NATURAL_END.test(bare)) return [250, 'natural ending'];
  if (words >= 3 && IMPERATIVE.test(s)) return [250, 'complete request'];
  if (/^(what|who|where|when|why|how|which|is|are|do|does|did|can|could|will|would|should)\b/i.test(s) && words >= 4) return [200, 'complete question'];
  if (words <= 2) return [900, 'very short fragment'];
  if (words >= 3) return [350, 'looks complete'];
  return [600, 'short statement'];
}
function takeSentences(buf, flush, firstChunk) {
  const out = []; let m;
  // the very first chunk may end at a clause (comma, dash, colon) once it has a few words, so speech starts sooner
  if (firstChunk && (m = /^([\s\S]{24,}?[,;:—–])(\s+)/.exec(buf)) && !/^[\s\S]*?[.!?…]/.test(m[1])) { out.push(m[1].trim()); buf = buf.slice(m[0].length); }
  while ((m = /^([\s\S]*?[.!?…]+["')\]]*)(\s+|$)/.exec(buf)) && (m[2] || flush) && m[1].trim().length >= 8) { out.push(m[1].trim()); buf = buf.slice(m[0].length); }
  if (flush && buf.trim()) { out.push(buf.trim()); buf = ''; }
  return [out, buf];
}

const normWords = (s) => s.toLowerCase().replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter(Boolean);
// Its own voice transcribed back (a late fragment after it finished): mostly words it just said.
function looksLikeOwnVoice(text, said) {
  const w = normWords(text); if (!w.length) return true;
  const ref = new Set(normWords(said || ""));
  return w.filter((x) => ref.has(x)).length / w.length >= 0.75;
}

export { endpointWait, isBackchannel, looksLikeOwnVoice, takeSentences };
